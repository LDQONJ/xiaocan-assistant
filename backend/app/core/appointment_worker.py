"""
霸王餐高精度预约与名额监听引擎 (Appointment & Restock Worker)
功能包含：
1. 未到时间点：倒计时 31 分钟发送预热提醒，倒计时 1 分钟启动高频就绪监听，到点准时/提前量毫秒级抢单
2. 已到时间点且有名额：直接即时抢单
3. 已到时间点但无名额：启动名额监听任务，定期轮询库存，检测到补仓/放单毫秒级秒抢，超时自动停，支持随时手动停止
"""
from __future__ import annotations
import asyncio
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List, Set

from ..models import database as db
from ..protocol.client import XiaoCanClient, XiaoCanRPCError, is_waf_blocked, get_waf_cooldown_seconds
from .scheduler import mark_task_started, mark_task_finished
from .notifier import send_system_notification
from .notification import NotificationEvent, NotificationLevel
from . import time_service

logger = logging.getLogger("xiaocan.appointment_worker")

client = XiaoCanClient()
_worker_task: Optional[asyncio.Task] = None
_is_running = False

# 内存热缓存 (Hot Cache)：将 50 次/秒高频自旋的磁盘 I/O 降低至每秒最多 1 次
_cached_active_appointments: list = []
_last_fetch_active_ts: float = 0.0
_cached_accounts: Dict[str, Any] = {}

# 任务运行时内存日志追踪 (避免高频写库，同时保留完整执行流水)
_apt_poll_counters: Dict[int, int] = {}
_apt_runtime_logs: Dict[int, list] = {}

def _append_apt_log(aid: int, log_id: int, text: str, sync_db: bool = False, status: Optional[str] = None):
    """追加任务步骤日志并有策略地同步回 SQLite 数据库"""
    if aid not in _apt_runtime_logs:
        _apt_runtime_logs[aid] = []
    _apt_runtime_logs[aid].append(text)
    
    # 限制单任务内存保留最多 60 条关键日志，防止无限增长
    if len(_apt_runtime_logs[aid]) > 60:
        _apt_runtime_logs[aid] = _apt_runtime_logs[aid][-50:]

    if log_id > 0 and (sync_db or status is not None):
        try:
            with db.get_conn() as conn:
                row = conn.execute("SELECT output FROM job_logs WHERE id = ?", (log_id,)).fetchone()
                prev_text = row["output"] if row else ""
            
            # 合并已有历史与新日志 (若已有历史，追加增量部分)
            full_output = (prev_text + "\n" + "\n".join(_apt_runtime_logs[aid])).strip()
            # 过滤相邻完全重复的多余行
            cleaned_lines = []
            for line in full_output.split("\n"):
                if not cleaned_lines or cleaned_lines[-1] != line:
                    cleaned_lines.append(line)
            # 保留最近 100 行
            final_text = "\n".join(cleaned_lines[-100:])
            db.update_job_log(log_id, status=status, output=final_text)
            # 同步完成后清空增量缓冲区
            _apt_runtime_logs[aid].clear()
        except Exception as e:
            logger.debug(f"同步任务 #{aid} 步骤日志到数据库异常: {e}")

def invalidate_worker_cache():
    global _last_fetch_active_ts
    _last_fetch_active_ts = 0.0


def _parse_time_today(time_str: str) -> Optional[float]:
    """解析 HH:MM 为今日的时间戳，若格式为 YYYY-MM-DD HH:MM:SS 或纯数字时间戳则直接解析"""
    if not time_str or not str(time_str).strip():
        return None
    time_str = str(time_str).strip()
    now = datetime.now()
    try:
        if time_str.replace('.', '', 1).isdigit():
            val = float(time_str)
            if val > 1e11:
                val = val / 1000.0
            if val > 1e8:
                return val
        if "-" in time_str:
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
        if ":" in time_str:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            s = int(parts[2]) if len(parts) > 2 else 0
            dt = datetime(now.year, now.month, now.day, h, m, s)
            return dt.timestamp()
    except Exception as e:
        logger.warning(f"解析时间戳失败 {time_str}: {e}")
    return None


def calculate_order_renew_schedule(
    order_time: int,
    activity_end_time_str: str = "",
    buffer_seconds: int = 300,
    check_now_ts: int = 0
) -> Dict[str, Any]:
    """
    动态计算待下单订单的下一次退单重抢续期时刻。
    核心规则：
    1. 正常续期点：下单时间 + 110 分钟 (6600 秒)；
    2. 安全底线点：商户活动今日结束时刻提前 buffer_seconds (默认 300 秒即 5 分钟)；
    3. 若正常续期点早于安全底线点：按正常 110 分钟续期，is_final=0；
    4. 若活动结束较早 (安全底线点 <= 正常续期点)：在活动结束前 5 分钟提前续期，标记 is_final=1 (末次续期，续完后自动终结)；
    5. 若当前已到达或超过安全底线点：无法安全续期，返回 can_renew=False。
    """
    now_ts = check_now_ts if check_now_ts > 0 else int(time.time())
    if order_time <= 0:
        order_time = now_ts

    normal_target = order_time + 6600

    activity_end_ts: Optional[float] = None
    if activity_end_time_str and str(activity_end_time_str).strip():
        activity_end_ts = _parse_time_today(str(activity_end_time_str).strip())

    if not activity_end_ts or activity_end_ts <= 0:
        now_dt = datetime.fromtimestamp(order_time)
        end_dt = datetime(now_dt.year, now_dt.month, now_dt.day, 23, 59, 59)
        activity_end_ts = end_dt.timestamp()

    safe_deadline = int(activity_end_ts) - buffer_seconds

    if now_ts >= safe_deadline:
        return {
            "can_renew": False,
            "next_renew_ts": 0,
            "is_final": 0,
            "reason": "当前已临近或超过商户活动安全截止时间，无法安全执行退单重抢"
        }

    if normal_target < safe_deadline:
        return {
            "can_renew": True,
            "next_renew_ts": normal_target,
            "is_final": 0,
            "reason": "计划在抢单满 110 分钟 (1小时50分) 时自动执行无损退单重抢"
        }
    else:
        end_display = time.strftime("%H:%M:%S", time.localtime(activity_end_ts))
        return {
            "can_renew": True,
            "next_renew_ts": safe_deadline,
            "is_final": 1,
            "reason": f"商户活动将于 {end_display} 结束，计划在活动结束前 5 分钟执行末次续期守护"
        }


async def execute_grab_for_appointment(apt: Dict[str, Any], account: Dict[str, Any], advance: bool = False, action_source: str = "store_grab") -> Dict[str, Any]:
    """执行小蚕官方抢单接口，并全程记录高精步骤日志入系统运行流水"""
    aid = apt.get("id") or 0
    token = account.get("token")
    silk_id = account.get("silk_id")
    user_id = account.get("user_id")
    city_code = account.get("city_code") or 440303
    account_key = apt.get("account_key") or account.get("key", "")
    nickname = account.get("nickname") or account_key
    store_name = apt.get("store_name", "店铺活动")
    promotion_id = apt.get("promotion_id", "")
    platform_name = "美团外卖" if apt.get("platform") == "meituan" else ("饿了么" if apt.get("platform") == "eleme" else "京东外卖")
    platform_code = int(apt.get("store_platform") or (3 if apt.get("platform") in ("jingdong", "jd") else (1 if apt.get("platform") == "meituan" else 2)))

    task_id = action_source if action_source in ("store_grab", "store_appoint", "store_monitor", "store_search", "store_cancel") else "store_grab"
    job_id = f"job_store_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    source_labels = {
        "store_grab": "即时抢单",
        "store_appoint": "定时预约",
        "store_monitor": "名额监听",
        "store_keyword": "店名嗅探",
        "store_search": "定向搜索",
        "store_cancel": "取消名额",
    }
    source_label = source_labels.get(action_source, "自动抢单")

    mark_task_started(account_key, task_id)
    try:
        log_steps = []
        t_start = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_steps.append(f"[{t_start}] 启动抢单流水: 店铺【{store_name}】(活动ID: {promotion_id}, 平台: {platform_name}, 触发方式: {source_label})")

        try:
            lat = float(account.get("latitude") or 0.0)
        except Exception:
            lat = 0.0
        try:
            lon = float(account.get("longitude") or 0.0)
        except Exception:
            lon = 0.0

        t_step1 = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_steps.append(f"[{t_step1}] 步骤 1/4: 账号鉴权及定位核验 - 账号【{nickname}】，定位经纬度 ({lat:.5f}, {lon:.5f})，城市代码 {city_code}")
        # 红包使用策略解析与防失效自愈降级 (优先使用提前 25 秒预加载并锁定的红包，消除开抢时的网络延时)
        redpack_id = apt.get("cached_redpack_id")
        redpack_mode = apt.get("redpack_mode", 0)
        rp_desc = "未使用"
        if redpack_id is not None:
            rp_desc = f"预锁定红包 #{redpack_id}"
        elif redpack_mode == 0:
            # 自动使用最高额度红包 (对齐微信小程序/省心版)
            try:
                max_rp = await client.get_user_max_redpack(token=token, silk_id=silk_id, user_id=user_id, city_code=city_code)
                rep_pack = max_rp.get("rep_pack") or {}
                if not rep_pack and max_rp.get("platform_red_packs"):
                    rep_pack = max_rp["platform_red_packs"][0]
                if rep_pack and rep_pack.get("user_red_pack_id"):
                    redpack_id = int(rep_pack["user_red_pack_id"])
                    rp_val = float(rep_pack.get("reward_num", 0) or 0) / 100
                    rp_desc = f"智能匹配最优红包 #{redpack_id} (金额: ¥{rp_val:.2f})"
                else:
                    rp_desc = "未找到可用平台红包，以无红包模式发起"
            except Exception as rpe:
                rp_desc = f"自动查询红包跳过: {rpe}"
        elif redpack_mode == 1:
            specified_rp_id = None
            if apt.get("redpack_id"):
                try:
                    specified_rp_id = int(apt["redpack_id"])
                except Exception:
                    pass

            if specified_rp_id:
                # 核验指定红包是否仍有效且未在移动端核销
                is_valid_specified = False
                unused_list = []
                try:
                    rp_res = await client.get_app_redpack_list(token=token, silk_id=silk_id, user_id=user_id, city_code=city_code, page=1, page_size=50)
                    unused_list = rp_res.get("unused_items") or []
                    for item in unused_list:
                        if int(item.get("user_red_pack_id") or 0) == specified_rp_id:
                            is_valid_specified = True
                            redpack_id = specified_rp_id
                            rp_name = item.get("name") or apt.get("redpack_name") or ""
                            item_val = float(item.get("reward_num", 0) or 0) / 100
                            rp_desc = f"指定红包 #{redpack_id} ({rp_name}, 金额: ¥{item_val:.2f})"
                            break
                except Exception as e:
                    logger.warning(f"核验指定红包有效性异常: {e}")
                    redpack_id = specified_rp_id
                    rp_desc = f"指定红包 #{redpack_id}"
                    is_valid_specified = True

                if not is_valid_specified:
                    # 触发防核销自愈降级替换：预选红包已在移动端被使用或过期
                    logger.warning(f"预约任务 #{aid} 指定红包 #{specified_rp_id} 已在端外使用或失效，启动平滑自愈替换...")
                    best_replacement = None
                    if unused_list:
                        sorted_unused = sorted(unused_list, key=lambda x: float(x.get("reward_num") or 0), reverse=True)
                        best_replacement = sorted_unused[0]

                    if best_replacement and best_replacement.get("user_red_pack_id"):
                        redpack_id = int(best_replacement["user_red_pack_id"])
                        best_val = float(best_replacement.get("reward_num", 0) or 0) / 100
                        rp_desc = f"预设红包 #{specified_rp_id} 已在移动端使用，已自愈平滑替换为当前最优红包 #{redpack_id} (金额: ¥{best_val:.2f})"
                    else:
                        redpack_id = None
                        rp_desc = f"预设红包 #{specified_rp_id} 已在移动端使用，当前无其他可用红包，自愈降级为无红包模式"
            else:
                rp_desc = "未指定有效红包ID，以无红包模式发起"
        else:
            rp_desc = "用户设置不使用红包"

        t_step2 = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_steps.append(f"[{t_step2}] 步骤 2/4: 红包策略决策 - {rp_desc}")

        try:
            t_step3 = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            log_steps.append(f"[{t_step3}] 步骤 3/4: 发起官方抢单 RPC 握手 - promotion_id={promotion_id}, store_platform={platform_code}, advance={advance}")

            res = await client.grab_promotion_quota(
                promotion_id=apt["promotion_id"],
                token=token,
                silk_id=silk_id,
                user_id=user_id,
                city_code=city_code,
                store_platform=platform_code,
                if_advance_order=advance,
                latitude=lat,
                longitude=lon,
                redpack_id=redpack_id,
                store_id=str(apt.get("store_id") or ""),
                store_name=str(apt.get("store_name") or ""),
                wm_poi_id=str(apt.get("wm_poi_id") or "")
            )
        
            # 严格校验小蚕官方订单编号 (成功时必须返回 promotion_order_id > 0)
            order_id = res.get("promotion_order_id") or (res.get("order") or {}).get("promotion_order_id") or 0
            try:
                order_id = int(order_id)
            except Exception:
                order_id = 0

            linked_log_id = int(apt.get("log_id") or 0)

            if order_id <= 0:
                err_msg = (res.get("status") or {}).get("msg") or res.get("msg") or "未返回有效订单号，官方抢单未生效"
                t_step4 = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                log_steps.append(f"[{t_step4}] 步骤 4/4: 官方接口返回未成功 - 提示: {err_msg}")
                log_steps.append(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 流程结束 - 抢单未锁定名额")
                logger.warning(f"预约/监听任务 #{aid} 抢单未成功: {err_msg}")
                grab_text = "\n".join(log_steps)
                if linked_log_id > 0:
                    _append_apt_log(aid, linked_log_id, grab_text, sync_db=True, status="error")
                else:
                    try:
                        db.add_job_log(job_id, account_key, task_id, "error", grab_text)
                    except Exception:
                        pass
                return {"ok": False, "code": -1, "message": err_msg}

            t_step4 = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            log_steps.append(f"[{t_step4}] 步骤 4/4: 抢单成功锁定 - 官方系统订单 #{order_id} 生成完毕！")
            success_msg = f"抢单成功锁定！官方系统订单 #{order_id} 生成完毕，请前往外卖平台下单"

            raw_pid_str = str(apt.get("promotion_id") or "").strip()
            clean_pid_str = re.sub(r"_p\d+$", "", raw_pid_str)
            try:
                pid_int = int(clean_pid_str)
            except Exception:
                pid_int = 0
            rebate_type_str = str(apt.get("rebate_type") or "").strip().lower()
            rebate_desc_str = str(apt.get("rebate_desc") or "").strip()
            is_percent_order = (
                rebate_type_str == "percent"
                or pid_int >= 10_000_000_000
                or "%" in rebate_desc_str
                or "比例" in rebate_desc_str
                or bool(res.get("user_ratio"))
                or bool(res.get("ratio"))
                or int(res.get("order_type") or 0) == 12
                or float(apt.get("order_money") or 0) <= 0
            )

            # 仅实付满返订单支持触发 App 原生「领红包延时 2 小时」任务特权；百分比/按比例返订单不支持该延时特权
            if not is_percent_order:
                try:
                    delay_task_type = 4 if int(platform_code) == 1 else 3
                    delay_res = await client.complete_task_event(
                        token=token,
                        silk_id=silk_id,
                        user_id=user_id,
                        city_code=city_code,
                        promotion_order_id=order_id,
                        task_type=delay_task_type
                    )
                    is_delayed = bool(delay_res.get("if_delay") or delay_res.get("if_delay_success") or delay_res.get("ifDelay") or delay_res.get("ifDelaySuccess"))
                    raw_d_time = int(delay_res.get("delay_time") or delay_res.get("delayTime") or 0)
                    if is_delayed or raw_d_time > 0:
                        d_time = raw_d_time if raw_d_time > 0 else 7200
                        log_steps.append(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 附加权益: 成功激活官方【领红包延时2小时】特权，凭据上传有效期已自动延长 2 小时 ({d_time}秒)")
                except Exception as de:
                    logger.info(f"领红包延时触发提示: {de}")

            log_steps.append(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 流程完成 - 订单状态已置为待下单，已投递抢单成功系统通知")
            logger.info(f"预约/监听任务 #{aid} 抢单成功: {success_msg}")

            # 写入订单库
            try:
                ratio_val = 0.0
                if is_percent_order:
                    raw_r = float(res.get("user_ratio") or res.get("ratio") or 0)
                    if raw_r > 0:
                        ratio_val = round(raw_r / 100.0 if raw_r >= 100 else raw_r, 1)
                    else:
                        m_ratio = re.search(r"(\d+(?:\.\d+)?)\s*%", rebate_desc_str)
                        if m_ratio:
                            ratio_val = float(m_ratio.group(1))
                delay_val = 1 if (is_delayed or raw_d_time > 0) else 0
                now_ts_order = int(time.time())
                tt_val = now_ts_order + d_time if (is_delayed or raw_d_time > 0) else now_ts_order + 7200
                exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tt_val))

                # 构建原始抢单上下文快照 (用于后续订单无损续期退单重抢)
                promo_end_time = str(apt.get("end_time") or apt.get("until_time") or "").strip()
                promo_params_data = {
                    "promotion_id": apt["promotion_id"],
                    "store_id": str(apt.get("store_id") or ""),
                    "store_name": str(apt.get("store_name") or ""),
                    "store_icon": str(apt.get("store_icon") or ""),
                    "platform_code": int(platform_code),
                    "platform": str(apt.get("platform") or "meituan"),
                    "end_time": promo_end_time,
                    "redpack_id": redpack_id,
                    "order_money": float(apt.get("order_money", 0.0)),
                    "rebate_price": float(apt.get("rebate_price", 0.0)),
                    "rebate_desc": str(apt.get("rebate_desc") or ""),
                    "rebate_type": str(apt.get("rebate_type") or "fixed"),
                    "wm_poi_id": str(apt.get("wm_poi_id") or "")
                }
                promo_params_json = json.dumps(promo_params_data, ensure_ascii=False)

                # 动态计算首次续期计划 (默认开启续期保护，若商家活动早于110分钟则在活动前5分钟末次续期)
                auto_renew_flag = int(apt.get("auto_renew_order") if apt.get("auto_renew_order") is not None else 1)
                sched = calculate_order_renew_schedule(order_time=now_ts_order, activity_end_time_str=promo_end_time)
                if auto_renew_flag and sched.get("can_renew"):
                    order_auto_renew = 1
                    order_next_renew_ts = sched["next_renew_ts"]
                    order_is_final = sched["is_final"]
                    order_renew_status = "scheduled"
                    order_renew_log = f"[{time_service.format_now_ms()}] 抢单成功，自动开启无损续期守护: {sched['reason']}"
                else:
                    order_auto_renew = 0
                    order_next_renew_ts = 0
                    order_is_final = 0
                    order_renew_status = "idle"
                    order_renew_log = ""

                saved_ord = db.save_order({
                    "account_key": apt["account_key"],
                    "order_sn": str(res.get("order_id_str") or order_id),
                    "promotion_order_id": order_id,
                    "store_id": apt["store_id"],
                    "store_name": apt["store_name"],
                    "store_icon": apt.get("store_icon") or "",
                    "platform": apt.get("platform", "meituan"),
                    "order_money": 0.0 if is_percent_order else float(apt.get("order_money", 0.0)),
                    "rebate_money": float(apt.get("rebate_price", 0.0)),
                    "order_type": 12 if is_percent_order else 1,
                    "rebate_ratio": ratio_val,
                    "status": "pending",
                    "condition": "无需评价",
                    "order_time": now_ts_order,
                    "timeout_time": tt_val,
                    "expire_time": exp_str,
                    "delay_count": delay_val,
                    "auto_renew": order_auto_renew,
                    "renew_count": 0,
                    "next_renew_ts": order_next_renew_ts,
                    "is_final_renew": order_is_final,
                    "renew_status": order_renew_status,
                    "renew_log": order_renew_log,
                    "promotion_params": promo_params_json
                })
            except Exception as oe:
                logger.warning(f"写入订单记录失败: {oe}")

            # 发送系统通知
            renew_notice_tail = " (已启用 110 分钟自动无损续期守护)" if order_auto_renew else ""
            await send_system_notification(
                title="小蚕霸王餐抢单成功",
                content=f"账号【{account.get('nickname')}】已成功抢到【{apt['store_name']}】({apt.get('rebate_desc', '')})，订单号 #{order_id}，请及时在平台下单{renew_notice_tail}",
                account_key=apt.get("account_key"),
                event=NotificationEvent.GRAB_SUCCESS,
                level=NotificationLevel.SUCCESS
            )

            if aid > 0:
                db.update_appointment(aid, {
                    "status": "success",
                    "outcome": success_msg
                })
                invalidate_worker_cache()

            grab_text = "\n".join(log_steps)
            if linked_log_id > 0:
                _append_apt_log(aid, linked_log_id, grab_text, sync_db=True, status="success")
            else:
                try:
                    db.add_job_log(job_id, account_key, task_id, "success", grab_text)
                except Exception:
                    pass

            return {"ok": True, "order_id": order_id, "message": success_msg}
        except XiaoCanRPCError as e:
            t_err = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            log_steps.append(f"[{t_err}] 官方接口响应异常: {e.msg} (错误码: {e.code})")
            logger.warning(f"抢单接口响应: code={e.code}, msg={e.msg}")
            grab_text = "\n".join(log_steps)
            linked_log_id = int(apt.get("log_id") or 0)
            if linked_log_id > 0:
                _append_apt_log(aid, linked_log_id, grab_text, sync_db=True, status="error")
            else:
                try:
                    db.add_job_log(job_id, account_key, task_id, "error", grab_text)
                except Exception:
                    pass
            if e.code == 401:
                try:
                    await send_system_notification(
                        title="账号登录凭据失效",
                        content=f"账号【{nickname}】在发起抢单时检测到登录凭据已过期或失效，抢单任务已自动挂起停止。请前往「账号管理」重新获取并更新 Token 凭据。",
                        account_key=account_key,
                        event=NotificationEvent.TOKEN_WARN,
                        level=NotificationLevel.WARNING
                    )
                except Exception:
                    pass
            return {"ok": False, "code": e.code, "message": e.msg}
        except Exception as e:
            t_err = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            log_steps.append(f"[{t_err}] 抢单接口网络异常: {str(e)}")
            logger.error(f"抢单接口网络异常: {e}")
            grab_text = "\n".join(log_steps)
            linked_log_id = int(apt.get("log_id") or 0)
            if linked_log_id > 0:
                _append_apt_log(aid, linked_log_id, grab_text, sync_db=True, status="error")
            else:
                try:
                    db.add_job_log(job_id, account_key, task_id, "error", grab_text)
                except Exception:
                    pass
            return {"ok": False, "code": -1, "message": str(e)}
    finally:
        mark_task_finished(account_key, task_id)


async def process_active_appointments() -> Tuple[bool, bool]:
    """单轮调度处理所有活跃的预约和监听任务，返回 (是否有临近 35 秒内的秒杀任务, 是否临近整分准点放单)"""
    global _cached_active_appointments, _last_fetch_active_ts, _cached_accounts
    now_ts = time_service.now_ts()

    # 高频自旋保护：每秒最多向磁盘发起 1 次活跃任务检索
    if now_ts - _last_fetch_active_ts >= 1.0 or not _cached_active_appointments:
        _cached_active_appointments = db.get_active_appointments()
        _last_fetch_active_ts = now_ts
        _cached_accounts.clear()

    active_list = _cached_active_appointments
    if not active_list:
        return False, False

    now_str = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")
    has_primed = False

    # 检测是否临近每分钟 00 秒放单时刻 (58.5s ~ 02.0s 窗口期)
    curr_min_start = int(now_ts // 60) * 60
    sec_in_min = now_ts - curr_min_start
    has_near_minute = False
    if any(a.get("status") in ("monitoring", "running", "waiting") for a in active_list):
        if sec_in_min >= 58.5 or sec_in_min < 2.0:
            has_near_minute = True

    for apt in active_list:
        aid = apt["id"]
        status = apt.get("status")
        task_type = apt.get("task_type") or "countdown"
        acc_key = apt["account_key"]

        if acc_key not in _cached_accounts:
            _cached_accounts[acc_key] = db.get_account_by_key(acc_key)
        account = _cached_accounts[acc_key]

        if not account or not account.get("token"):
            db.update_appointment(aid, {
                "status": "failed",
                "outcome": "对应账号凭据失效或已被删除"
            })
            invalidate_worker_cache()
            continue

        # ---------------- 分支一：店名抢单监听与智能嗅探 (name_monitor / keyword) ---------------- #
        if task_type in ("name_monitor", "keyword", "keyword_monitor"):
            linked_log_id = int(apt.get("log_id") or 0)
            # 1. 检查是否达到开始时间
            start_ts = _parse_time_today(apt.get("start_time"))
            if start_ts and now_ts < start_ts:
                continue

            # 2. 检查是否超过截止时间
            until_ts = _parse_time_today(apt.get("until_time"))
            if until_ts and now_ts >= until_ts:
                outcome_msg = f"已达到设定的店名监听截止时间 {apt.get('until_time')}，自动结束"
                db.update_appointment(aid, {
                    "status": "expired",
                    "outcome": outcome_msg
                })
                invalidate_worker_cache()
                if linked_log_id > 0:
                    t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                    _append_apt_log(aid, linked_log_id, f"[{t_now}] 店名监听截止时间到达 ({apt.get('until_time')})，监听任务自动结束", sync_db=True, status="error")
                continue

            # 3. 检查频率与每分钟 00 秒准点放单对齐机制
            # 算法核心：无论设置的轮询频率为何值，从 00:00 开始的每 1 分钟整点 (00秒) 均保证进行一次嗅探
            interval = max(3, int(apt.get("check_interval") or 10))
            last_checked = _parse_time_today(apt.get("last_checked_at"))
            is_on_minute_probe = False

            # 分支 A: 整分 00 秒准点放单保底命中 (00~02.5秒区间且当前分钟尚未执行过嗅探)
            if sec_in_min < 2.5 and (not last_checked or last_checked < curr_min_start):
                is_on_minute_probe = True
            # 分支 B: 临近分钟末尾避让保护 (57.5~59.9秒区间，避免抢跑导致 00 秒被限流，主动延后至 00 秒准点发射)
            elif sec_in_min >= 57.5:
                continue
            # 分支 C: 常规轮询间隔触发
            elif last_checked and (now_ts - last_checked) < interval:
                continue

            # 如果当前网络处于 WAF 熔断冷却期，主动跳过嗅探，避免延长封锁
            if is_waf_blocked():
                remain = get_waf_cooldown_seconds()
                if (int(now_ts) % 30) < 2 and linked_log_id > 0:
                    t_now = time_service.format_now_ms()
                    _append_apt_log(aid, linked_log_id, f"[{t_now}] [风控熔断守护] 当前网络IP处于官方腾讯云WAF临时拦截冷却中 (剩余约 {remain} 秒)，已自动暂停嗅探以避免延长封锁。切换手机热点或重启光猫更换IP可立即恢复。", sync_db=True, status="running")
                continue

            apt["last_checked_at"] = now_str
            db.update_appointment(aid, {"last_checked_at": now_str})

            # 4. 执行双源嗅探与多档位优选
            _apt_poll_counters[aid] = _apt_poll_counters.get(aid, 0) + 1
            poll_cnt = _apt_poll_counters[aid]

            # 防风控随机微抖动：准点对齐嗅探零延迟直射开火，常规轮询时微抖动 0.05~0.15s
            if not is_on_minute_probe:
                await asyncio.sleep(random.uniform(0.05, 0.15))

            kw = (apt.get("keyword") or apt.get("store_name") or "").strip()
            match_mode = apt.get("match_mode") or "contains"
            target_plat = apt.get("platform") or "all"
            rebate_mode_filter = apt.get("rebate_mode_filter") or "all"
            min_rebate_price = float(apt.get("min_rebate_price") or 0.0)
            min_rebate_rate = float(apt.get("min_rebate_rate") or 0.0)
            max_order_money = float(apt.get("max_order_money") or 0.0)
            auto_stop = int(apt.get("auto_stop_on_success") if apt.get("auto_stop_on_success") is not None else 1)

            token = account.get("token")
            silk_id = account.get("silk_id")
            user_id = account.get("user_id")
            city_code = int(account.get("city_code") or 440300)
            lon = str(account.get("longitude") or "114.13166")
            lat = str(account.get("latitude") or "22.548361")

            try:
                from ..api.endpoints import _extract_all_promos_from_raw, _normalize_shangjin_item

                search_tasks = [
                    client.search_stores(
                        keyword=kw,
                        city_code=city_code,
                        longitude=lon,
                        latitude=lat,
                        offset=0,
                        limit=20,
                        token=token,
                        silk_id=silk_id,
                        user_id=user_id
                    )
                ]
                has_sj = False
                if target_plat in ("all", "meituan"):
                    has_sj = True
                    search_tasks.append(
                        client.search_shangjin_stores(
                            keyword=kw,
                            latitude=float(lat),
                            longitude=float(lon),
                            token=token,
                            silk_id=silk_id,
                            user_id=user_id,
                            city_code=city_code,
                            sort_type=3
                        )
                    )

                results = await asyncio.gather(*search_tasks, return_exceptions=True)
                search_res = results[0] if len(results) > 0 and not isinstance(results[0], Exception) else {}
                sj_res = results[1] if has_sj and len(results) > 1 and not isinstance(results[1], Exception) else {}

                candidates = []
                # 解析常规活动列表
                raw_proms = search_res.get("promotions") or search_res.get("promotion_list") or search_res.get("feed_items") or []
                for p in raw_proms:
                    extracted = _extract_all_promos_from_raw(p, is_search=True, user_lat=float(lat), user_lon=float(lon))
                    candidates.extend(extracted)

                # 解析美团赏金按比例活动
                if isinstance(sj_res, dict):
                    pois = sj_res.get("poi_list") or []
                    for poi in pois:
                        sj_items = _normalize_shangjin_item(poi, user_lat=float(lat), user_lon=float(lon))
                        candidates.extend(sj_items)

                # 规则过滤与匹配
                matched_candidates = []
                total_found_stores = set()
                kw_lower = kw.lower()

                for c in candidates:
                    s_name = (c.get("name") or "").strip()
                    if not s_name:
                        continue
                    total_found_stores.add(s_name)

                    # 1. 店名匹配规则
                    if match_mode == "exact":
                        if s_name.lower() != kw_lower:
                            continue
                    else:
                        if kw_lower not in s_name.lower():
                            continue

                    # 2. 平台过滤
                    c_plat = c.get("platform")
                    if target_plat != "all" and c_plat != target_plat:
                        continue

                    # 3. 返利模式过滤 (all / fixed / percent)
                    c_rtype = c.get("rebate_type")
                    if rebate_mode_filter != "all" and c_rtype != rebate_mode_filter:
                        continue

                    # 4. 最低返利金额
                    c_rebate_price = float(c.get("rebate_price") or 0.0)
                    if min_rebate_price > 0 and c_rebate_price < min_rebate_price:
                        continue

                    # 5. 最低返利比例
                    c_rebate_rate = float(c.get("rebate_rate") or 0.0)
                    if min_rebate_rate > 0 and c_rebate_rate < min_rebate_rate:
                        continue

                    # 6. 最高起送/门槛金额
                    c_order_money = float(c.get("order_money") or 0.0)
                    if max_order_money > 0 and c_order_money > max_order_money:
                        continue

                    # 7. 库存名额
                    c_left = int(c.get("left_number") or 0)
                    if c_left <= 0:
                        continue

                    matched_candidates.append(c)

                # 按最优返利比排序 (优先返利金额最高，次选返利比例最高)
                matched_candidates.sort(
                    key=lambda x: (float(x.get("rebate_price") or 0.0), float(x.get("rebate_rate") or 0.0)),
                    reverse=True
                )

                # 区分即刻可抢方案与等待开抢方案，防止未到开抢时间盲目调用官方接口导致“活动未开始”错误无限重复
                grabbable_candidates = []
                waiting_candidates = []
                use_adv = bool(apt.get("use_advance_card"))

                for c in matched_candidates:
                    c_start_val = c.get("start_time") or c.get("start_date_timestamp") or c.get("start_timestamp")
                    c_start_ts = _parse_time_today(str(c_start_val)) if c_start_val else None
                    c_end_val = c.get("end_time") or c.get("end_date_timestamp") or c.get("end_timestamp")
                    c_end_ts = _parse_time_today(str(c_end_val)) if c_end_val else None

                    if c_end_ts and now_ts >= c_end_ts:
                        # 方案已过活动结束时间
                        continue

                    is_ready = False
                    is_adv_mode = False
                    wait_reason = ""

                    if not c_start_ts:
                        is_ready = True
                    elif now_ts >= c_start_ts - 1.0:
                        # 正点开抢时刻 (允许 1 秒提前对齐)
                        is_ready = True
                        is_adv_mode = False
                    elif use_adv:
                        # 开启了超前抢单券，检查是否进入开抢前 30 分钟窗口
                        adv_start_ts = c_start_ts - 30 * 60
                        if now_ts >= adv_start_ts - 1.0:
                            is_ready = True
                            is_adv_mode = True
                        else:
                            is_ready = False
                            adv_time_str = datetime.fromtimestamp(adv_start_ts).strftime("%H:%M")
                            start_time_disp = str(c.get("start_time") or datetime.fromtimestamp(c_start_ts).strftime("%H:%M"))
                            wait_reason = f"尚未进入超前抢单窗口 (超前开抢: {adv_time_str}, 正式开抢: {start_time_disp})"
                    else:
                        is_ready = False
                        start_time_disp = str(c.get("start_time") or datetime.fromtimestamp(c_start_ts).strftime("%H:%M"))
                        wait_reason = f"尚未到达开抢时间 (开抢时间: {start_time_disp})"

                    if is_ready:
                        grabbable_candidates.append((c, is_adv_mode))
                    else:
                        waiting_candidates.append((c, wait_reason))

                t_now = time_service.format_now_ms()
                probe_tag = " (整分准点放单对齐)" if is_on_minute_probe else ""

                if grabbable_candidates:
                    best_item, is_adv = grabbable_candidates[0]
                    store_label = best_item.get("name", kw)
                    rebate_label = best_item.get("rebate_desc", "")
                    adv_tag = "【超前券·提前抢单】" if is_adv else ""
                    step_str = f"[{t_now}] 第 {poll_cnt} 次嗅探成功{probe_tag}: 检索到「{store_label}」有可抢名额 ({rebate_label}, 剩余库存 {best_item.get('left_number')} 份)！{adv_tag}立即触发闪电抢单！"
                    logger.info(f"店名监听 #{aid} 命中可用方案【{store_label}】({rebate_label})，立即触发抢单！")
                    if linked_log_id > 0:
                        _append_apt_log(aid, linked_log_id, step_str, sync_db=True, status="running")

                    grab_apt = dict(apt)
                    grab_apt["promotion_id"] = str(best_item.get("promotion_id", ""))
                    grab_apt["store_id"] = str(best_item.get("store_id", "0"))
                    grab_apt["store_name"] = store_label
                    grab_apt["store_icon"] = best_item.get("store_icon", "")
                    grab_apt["platform"] = best_item.get("platform", "meituan")
                    grab_apt["store_platform"] = int(best_item.get("store_platform") or (1 if best_item.get("platform") == "meituan" else 2))
                    grab_apt["order_money"] = float(best_item.get("order_money") or 0.0)
                    grab_apt["rebate_price"] = float(best_item.get("rebate_price") or 0.0)
                    grab_apt["rebate_desc"] = rebate_label
                    grab_apt["rebate_type"] = best_item.get("rebate_type", "fixed")

                    res = await execute_grab_for_appointment(grab_apt, account, advance=is_adv, action_source="store_monitor")
                    t_grab = time_service.format_now_ms()
                    if res.get("ok"):
                        succ_msg = f"抢单成功: 【{store_label}】{rebate_label}"
                        logger.info(f"店名监听 #{aid} {succ_msg}")
                        if auto_stop:
                            db.update_appointment(aid, {
                                "status": "completed",
                                "outcome": succ_msg,
                                "store_name": store_label,
                                "promotion_id": str(best_item.get("promotion_id", ""))
                            })
                            invalidate_worker_cache()
                            if linked_log_id > 0:
                                _append_apt_log(aid, linked_log_id, f"[{t_grab}] {succ_msg}，任务已达成并自动停止", sync_db=True, status="success")
                        else:
                            if linked_log_id > 0:
                                _append_apt_log(aid, linked_log_id, f"[{t_grab}] {succ_msg}，保持持续监听模式", sync_db=True, status="running")
                    else:
                        fail_msg = res.get("message") or "名额被抢占或网络响应延迟"
                        logger.warning(f"店名监听 #{aid} 抢单尝试未成功: {fail_msg}")
                        if res.get("code") == 401 or "401" in str(fail_msg):
                            db.update_appointment(aid, {"status": "error", "outcome": fail_msg})
                            invalidate_worker_cache()
                            if linked_log_id > 0:
                                _append_apt_log(aid, linked_log_id, f"[{t_grab}] 账号鉴权失效: {fail_msg}，任务已自动终止，请在「账号管理」中更新Token", sync_db=True, status="error")
                            await send_system_notification(
                                title="小蚕账号凭据失效",
                                content=f"账号【{apt.get('nickname') or account_key}】鉴权失效 (HTTP 401)，任务已停止，请重新提取或更新 Token。",
                                account_key=account_key,
                                event=NotificationEvent.TOKEN_WARN,
                                level=NotificationLevel.WARNING
                            )
                        elif linked_log_id > 0:
                            _append_apt_log(aid, linked_log_id, f"[{t_grab}] 抢单尝试返回: {fail_msg}，继续保持高频蹲守", sync_db=True, status="running")
                elif waiting_candidates:
                    waiting_item, wait_reason = waiting_candidates[0]
                    w_name = waiting_item.get("name", kw)
                    w_rebate = waiting_item.get("rebate_desc", "")
                    w_left = waiting_item.get("left_number", 0)
                    step_str = f"[{t_now}] 第 {poll_cnt} 次嗅探{probe_tag}: 检索到「{w_name}」有预留名额 ({w_rebate}, 剩余库存 {w_left} 份)，{wait_reason}，静默蹲守中..."
                    if linked_log_id > 0:
                        _append_apt_log(aid, linked_log_id, step_str, sync_db=(poll_cnt == 1 or poll_cnt % 3 == 0 or is_on_minute_probe), status="running")
                else:
                    # 未找到符合条件的方案
                    store_count = len(total_found_stores)
                    step_str = f"[{t_now}] 第 {poll_cnt} 次嗅探{probe_tag}: 关键词「{kw}」，发现 {store_count} 家商户 (当前暂无满足条件的剩余名额)，持续保持监听..."
                    if linked_log_id > 0:
                        _append_apt_log(aid, linked_log_id, step_str, sync_db=(poll_cnt == 1 or poll_cnt % 3 == 0 or is_on_minute_probe), status="running")

            except Exception as se:
                logger.warning(f"店名监听 #{aid} 执行异常: {se}")
                is_401 = (getattr(se, "code", None) == 401) or ("401" in str(se) and "Unauthorized" in str(se))
                if is_401:
                    err_msg = getattr(se, "msg", None) or "登录凭据已过期或鉴权失效 (HTTP 401 Unauthorized)"
                    db.update_appointment(aid, {"status": "error", "outcome": err_msg})
                    invalidate_worker_cache()
                    if linked_log_id > 0:
                        t_err = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        _append_apt_log(aid, linked_log_id, f"[{t_err}] 账号鉴权失效: {err_msg}，任务已自动终止，请更新Token", sync_db=True, status="error")
                    await send_system_notification(
                        title="小蚕账号凭据失效",
                        content=f"账号【{apt.get('nickname') or account_key}】鉴权失效 (HTTP 401)，监听已自动挂起停止，请重新更新凭据。",
                        account_key=account_key,
                        event=NotificationEvent.TOKEN_WARN,
                        level=NotificationLevel.WARNING
                    )
                    continue

                is_waf = is_waf_blocked() or (getattr(se, "code", None) == 403) or ("WAF" in str(se))
                if is_waf:
                    cooldown = get_waf_cooldown_seconds() or 180
                    future_time = datetime.fromtimestamp(now_ts + cooldown).strftime("%Y-%m-%d %H:%M:%S")
                    db.update_appointment(aid, {"last_checked_at": future_time})
                    if linked_log_id > 0:
                        t_err = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        _append_apt_log(aid, linked_log_id, f"[{t_err}] [风控熔断] 当前网络IP触发官方腾讯云WAF临时频次拦截，系统已自动休眠熔断保护 (剩余冷却约 {cooldown} 秒)。建议切换手机热点或重启光猫更换IP立即恢复。", sync_db=True, status="running")
                else:
                    if linked_log_id > 0:
                        t_err = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        _append_apt_log(aid, linked_log_id, f"[{t_err}] 嗅探执行异常: {se}，将在下次周期重试", sync_db=False, status="running")

        # ---------------- 分支二：单店名额监听捡漏任务 (task_type == "monitor" or status == "monitoring") ---------------- #
        elif task_type == "monitor" or status == "monitoring":
            linked_log_id = int(apt.get("log_id") or 0)
            # 1. 检查是否达到截止时间
            until_ts = _parse_time_today(apt.get("until_time"))
            if until_ts and now_ts >= until_ts:
                outcome_msg = f"已达到设定的监听截止时间 {apt.get('until_time')}，自动结束监听"
                db.update_appointment(aid, {
                    "status": "expired",
                    "outcome": outcome_msg
                })
                logger.info(f"监听任务 #{aid} 已超时结束 (until_time={apt.get('until_time')})")
                if linked_log_id > 0:
                    exp_line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] 监听截止时间已到达 ({apt.get('until_time')})，名额监听自动停止"
                    _append_apt_log(aid, linked_log_id, exp_line, sync_db=True, status="error")
                continue

            # 2. 控制检查频率与整分准点放单对齐
            interval = max(3, int(apt.get("check_interval") or 5))
            last_checked = _parse_time_today(apt.get("last_checked_at"))
            is_on_minute_probe = False

            if sec_in_min < 2.5 and (not last_checked or last_checked < curr_min_start):
                is_on_minute_probe = True
            elif sec_in_min >= 57.5:
                continue
            elif last_checked and (now_ts - last_checked) < interval:
                continue

            # 如果当前网络处于 WAF 熔断冷却期，主动跳过库存查询，避免延长封锁
            if is_waf_blocked():
                remain = get_waf_cooldown_seconds()
                if (int(now_ts) % 30) < 2 and linked_log_id > 0:
                    t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                    _append_apt_log(aid, linked_log_id, f"[{t_now}] [风控熔断守护] 当前网络IP处于官方腾讯云WAF临时拦截冷却中 (剩余约 {remain} 秒)，已自动暂停库存轮询以避免延长封锁。", sync_db=True, status="running")
                continue

            # 更新最后检查时间戳
            apt["last_checked_at"] = now_str
            db.update_appointment(aid, {"last_checked_at": now_str})

            # 3. 查验最新实时库存
            _apt_poll_counters[aid] = _apt_poll_counters.get(aid, 0) + 1
            poll_cnt = _apt_poll_counters[aid]
            try:
                detail = await client.get_store_promotion_detail(
                    promotion_id=apt["promotion_id"],
                    token=account["token"],
                    silk_id=account.get("silk_id"),
                    user_id=account.get("user_id"),
                    city_code=account.get("city_code", 440303),
                    store_name=str(apt.get("store_name") or ""),
                    latitude=float(account.get("latitude") or 22.54836),
                    longitude=float(account.get("longitude") or 114.13166)
                )
                pdetail = detail.get("promotion_detail") or {}
                mt_left = pdetail.get("meituan_left_number", 0)
                ele_left = pdetail.get("eleme_left_number", 0)
                plat = apt.get("platform", "meituan")
                target_left = mt_left if plat == "meituan" else ele_left
                if target_left is None or target_left == 0:
                    target_left = pdetail.get("left_number", 0)

                t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                step_str = f"[{t_now}] 第 {poll_cnt} 次名额检测: 美团余量 {mt_left or 0} 份 / 饿了么余量 {ele_left or 0} 份，持续监听中..."
                # 每轮巡检追加，且首轮或每 3 次或有名额变动时向数据库落盘一次
                sync_needed = (poll_cnt == 1) or (poll_cnt % 3 == 0) or (target_left and int(target_left) > 0)
                if linked_log_id > 0:
                    _append_apt_log(aid, linked_log_id, step_str, sync_db=sync_needed, status="running")

                if target_left and int(target_left) > 0:
                    start_time_str = apt.get("start_time") or pdetail.get("start_time")
                    start_ts = _parse_time_today(str(start_time_str)) if start_time_str else None
                    use_adv = bool(apt.get("use_advance_card"))

                    can_grab_now = False
                    is_adv = False
                    wait_reason = ""

                    if not start_ts:
                        can_grab_now = True
                    elif now_ts >= start_ts - 1.0:
                        can_grab_now = True
                        is_adv = False
                    elif use_adv:
                        adv_start_ts = start_ts - 30 * 60
                        if now_ts >= adv_start_ts - 1.0:
                            can_grab_now = True
                            is_adv = True
                        else:
                            can_grab_now = False
                            adv_time_str = datetime.fromtimestamp(adv_start_ts).strftime("%H:%M")
                            wait_reason = f"尚未进入超前抢单窗口 (超前开抢: {adv_time_str}, 正式开抢: {start_time_str})"
                    else:
                        can_grab_now = False
                        wait_reason = f"尚未到达开抢时间 (开抢时间: {start_time_str})"

                    if can_grab_now:
                        adv_tag = "【超前券·提前抢单】" if is_adv else ""
                        fire_str = f"[{t_now}] 捕获到目标名额放单 (剩余: {target_left} 份)！{adv_tag}立即触发闪电抢单..."
                        logger.info(f"监听任务 #{aid} 捕获到名额放单 (剩余: {target_left})！{adv_tag}立即触发闪电抢单...")
                        if linked_log_id > 0:
                            _append_apt_log(aid, linked_log_id, fire_str, sync_db=True, status="running")
                        res = await execute_grab_for_appointment(apt, account, advance=is_adv, action_source="store_monitor")
                        if not res["ok"]:
                            logger.warning(f"监听捕获后抢单失败: {res['message']}，继续保持监听")
                    else:
                        wait_str = f"[{t_now}] 第 {poll_cnt} 次名额检测: 发现剩余库存 {target_left} 份，但{wait_reason}，静默蹲守中..."
                        logger.info(f"监听任务 #{aid} {wait_str}")
                        if linked_log_id > 0:
                            _append_apt_log(aid, linked_log_id, wait_str, sync_db=(poll_cnt == 1 or poll_cnt % 3 == 0 or is_on_minute_probe), status="running")
            except Exception as e:
                logger.debug(f"轮询活动实时库存异常 (非致命): {e}")
                is_waf = is_waf_blocked() or (getattr(e, "code", None) == 403) or ("WAF" in str(e))
                if is_waf:
                    cooldown = get_waf_cooldown_seconds() or 180
                    future_time = datetime.fromtimestamp(now_ts + cooldown).strftime("%Y-%m-%d %H:%M:%S")
                    db.update_appointment(aid, {"last_checked_at": future_time})
                    if linked_log_id > 0:
                        t_err = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        _append_apt_log(aid, linked_log_id, f"[{t_err}] [风控熔断] 当前网络IP触发官方腾讯云WAF临时频次拦截，系统已自动休眠熔断保护 (剩余冷却约 {cooldown} 秒)", sync_db=True, status="running")

        # ---------------- 分支三：倒计时预约任务 (status in 'scheduled', 'primed', 'pending') ---------------- #
        elif status in ("scheduled", "primed", "pending"):
            linked_log_id = int(apt.get("log_id") or 0)
            start_ts = _parse_time_today(apt.get("start_time"))
            if not start_ts:
                continue

            use_advance = bool(apt.get("use_advance_card"))
            # 开启超前抢单券时，开抢时刻提前 30 分钟 (1800 秒)
            effective_start_ts = (start_ts - 30 * 60) if use_advance else start_ts
            diff = effective_start_ts - now_ts
            early_sec = max(0.0, float(apt.get("early_ms") or 500) / 1000.0)

            # 标记是否有临近开抢任务 (若在 35 秒内，开启 20ms 自适应超高精度监测)
            if 0 < diff <= 35:
                has_primed = True

            eff_time_str = datetime.fromtimestamp(effective_start_ts).strftime("%H:%M")
            adv_prefix = "【超前抢单·提前30分钟】" if use_advance else ""

            # 1. 倒计时 31 分钟预热通知提醒
            if diff <= (31 * 60) and not apt.get("notified_31m"):
                db.update_appointment(aid, {"notified_31m": 1})
                logger.info(f"任务 #{aid} 触发倒计时 31 分钟预热通知 (超前券: {use_advance})")
                t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                if linked_log_id > 0:
                    _append_apt_log(aid, linked_log_id, f"[{t_now}] 倒计时 31 分钟到达，已发送开火预热通知与通道就绪检查 {adv_prefix}(开火目标: {eff_time_str})", sync_db=True, status="running")
                try:
                    await send_system_notification(
                        title="霸王餐开抢预热提醒",
                        content=f"你预约的【{apt['store_name']}】({apt.get('rebate_desc', '')}) 还有 31 分钟即将开抢 {adv_prefix}（执行时间: {eff_time_str}），抢单通道已就绪",
                        account_key=apt.get("account_key"),
                        event=NotificationEvent.APPOINT_EVENT,
                        level=NotificationLevel.INFO
                    )
                except Exception as ne:
                    logger.warning(f"发送系统预热通知失败: {ne}")

            # 2. 倒计时 25 秒：长连接热激活 + 提前解析并锁定最高可用红包 (消除开火网络耗时)
            if diff <= 25 and not apt.get("prewarmed"):
                apt["prewarmed"] = 1
                db.update_appointment(aid, {"status": "primed"})
                logger.info(f"预约任务 #{aid} 提前 25 秒唤醒预热，长连接热激活并提前解析锁定红包...")
                t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                if linked_log_id > 0:
                    _append_apt_log(aid, linked_log_id, f"[{t_now}] 提前 25 秒唤醒：长连接握手热激活，预检锁定红包资产，微秒自旋压枪...", sync_db=True, status="running")
                try:
                    # 提前探测一次连接与锁定最优红包
                    await client.get_user_info(token=account["token"], silk_id=account.get("silk_id"), user_id=account.get("user_id"), city_code=account.get("city_code", 440303))
                    if apt.get("redpack_mode", 0) == 0 and not apt.get("cached_redpack_id"):
                        max_rp = await client.get_user_max_redpack(token=account["token"], silk_id=account.get("silk_id"), user_id=account.get("user_id"), city_code=account.get("city_code", 440303))
                        rep_pack = max_rp.get("rep_pack") or {}
                        if not rep_pack and max_rp.get("platform_red_packs"):
                            rep_pack = max_rp["platform_red_packs"][0]
                        if rep_pack and rep_pack.get("user_red_pack_id"):
                            apt["cached_redpack_id"] = int(rep_pack["user_red_pack_id"])
                            logger.info(f"预约任务 #{aid} 已提前锁定最优红包 #{apt['cached_redpack_id']}")
                    elif apt.get("redpack_mode") == 1 and apt.get("redpack_id") and not apt.get("cached_redpack_id"):
                        spec_id = int(apt["redpack_id"])
                        rplist_res = await client.get_app_redpack_list(token=account["token"], silk_id=account.get("silk_id"), user_id=account.get("user_id"), city_code=account.get("city_code", 440303), page=1, page_size=50)
                        unused = rplist_res.get("unused_items") or []
                        matched = any(int(item.get("user_red_pack_id") or 0) == spec_id for item in unused)
                        if matched:
                            apt["cached_redpack_id"] = spec_id
                            logger.info(f"预约任务 #{aid} 已提前核验并锁定指定红包 #{spec_id}")
                        else:
                            if unused:
                                sorted_unused = sorted(unused, key=lambda x: float(x.get("reward_num") or 0), reverse=True)
                                apt["cached_redpack_id"] = int(sorted_unused[0]["user_red_pack_id"])
                                logger.info(f"预约任务 #{aid} 预设红包 #{spec_id} 已在端外失效，提前自愈替换为最优红包 #{apt['cached_redpack_id']}")
                except Exception as rpe:
                    logger.debug(f"预约任务提前解析红包异常: {rpe}")

            # 3. 准点 / 提前量到达 (diff <= early_sec)：突发毫秒抢单！
            if diff <= early_sec:
                adv_log = "【超前抢单提前30分钟】" if use_advance else ""
                logger.info(f"任务 #{aid} 抢单时刻到达 (提前量 {early_sec}s, {adv_log})，发起突发毫秒抢单！")
                t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                if linked_log_id > 0:
                    _append_apt_log(aid, linked_log_id, f"[{t_now}] 预定开抢时刻到达 (提前量 {early_sec*1000:.0f}ms, {adv_log})，全速发射抢单报文！", sync_db=True, status="running")
                grab_ok = False
                last_msg = "抢单未生效"
                for try_idx in range(1, 4):
                    ts_fmt = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                    res = await execute_grab_for_appointment(apt, account, advance=use_advance, action_source="store_appoint")
                    if res.get("ok"):
                        logger.info(f"[{ts_fmt}] 任务 #{aid} 第{try_idx}次抢单成功！")
                        grab_ok = True
                        break
                    else:
                        last_msg = res.get("message") or "名额已满或尚未开仓"
                        logger.warning(f"[{ts_fmt}] 任务 #{aid} 第{try_idx}次抢单提示: {last_msg}")
                        if res.get("code") in (40003, 40004, 40037, 40038, 40039, 40040):
                            break
                    await asyncio.sleep(0.06)

                if not grab_ok:
                    until_str = apt.get("until_time")
                    if until_str:
                        db.update_appointment(aid, {
                            "status": "monitoring",
                            "outcome": f"首轮抢单未锁定 ({last_msg})，已自动切换为持续捡漏监听模式（持续至 {until_str}）"
                        })
                        logger.info(f"任务 #{aid} 首轮抢单未成功，已自动转入持续监听捡漏至 {until_str}")
                        if linked_log_id > 0:
                            t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                            _append_apt_log(aid, linked_log_id, f"[{t_now}] 首轮抢单未锁定 ({last_msg})，已自适应无缝转入实时名额监听捡漏模式（截止 {until_str}）...", sync_db=True, status="running")
                    else:
                        db.update_appointment(aid, {
                            "status": "failed",
                            "outcome": f"抢单失败: {last_msg}"
                        })
                        if linked_log_id > 0:
                            t_now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                            _append_apt_log(aid, linked_log_id, f"[{t_now}] 抢单流程结束: {last_msg}", sync_db=True, status="error")

    return has_primed, has_near_minute


_renewing_order_ids: Set[int] = set()


async def ensure_order_promotion_params(
    order_record: Dict[str, Any],
    cli: Optional[XiaoCanClient] = None,
    account: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    自愈探测并补全订单缺失的抢单上下文参数 (兼容历史订单或在微信小程序端手动抢单的订单)
    """
    params_str = order_record.get("promotion_params") or "{}"
    promo_params: Dict[str, Any] = {}
    try:
        promo_params = json.loads(params_str)
    except Exception:
        promo_params = {}

    if promo_params.get("promotion_id"):
        return promo_params

    acc = account
    if not acc:
        account_key = order_record.get("account_key")
        acc = db.get_account_by_key(account_key) if account_key else None
    if not acc or not acc.get("token"):
        return promo_params

    client_inst = cli or client
    token = acc["token"]
    silk_id = acc.get("silk_id")
    user_id = acc.get("user_id")
    city_code = int(acc.get("city_code") or 440303)
    target_promo_order_id = int(order_record.get("promotion_order_id") or 0)
    target_sn = str(order_record.get("order_sn") or "")
    store_name_str = str(order_record.get("store_name") or "")
    store_id_str = str(order_record.get("store_id") or "")
    lat = str(acc.get("latitude") or "22.548361")
    lon = str(acc.get("longitude") or "114.13166")

    # 第一级自愈：通过官方 GetPromotionOrderList 反查活动详情
    try:
        rpc_res = await client_inst.get_order_list(
            token=token,
            silk_id=silk_id,
            user_id=user_id,
            order_status=99,
            offset=0,
            number=30,
            city_code=city_code
        )
        raw_orders = rpc_res.get("order_list") or rpc_res.get("orders") or []
        matched_raw = None
        for o in raw_orders:
            p_oid = int(o.get("promotion_order_id") or 0)
            o_sn = str(o.get("order_id_str") or o.get("promotion_order_id") or "")
            if (target_promo_order_id > 0 and p_oid == target_promo_order_id) or (target_sn and o_sn == target_sn):
                matched_raw = o
                break

        if matched_raw:
            sp = matched_raw.get("store_promotion") or {}
            st_obj = sp.get("store") or {}
            found_pid = str(
                sp.get("promotion_id")
                or matched_raw.get("promotion_id")
                or sp.get("id")
                or ""
            )
            store_id = str(st_obj.get("store_id") or sp.get("store_id") or store_id_str or "")
            store_name = str(st_obj.get("name") or sp.get("store_name") or store_name_str or "")
            plat_code = int(matched_raw.get("store_platform") or sp.get("store_platform") or (1 if order_record.get("platform") == "meituan" else 2))
            end_time = str(sp.get("end_time") or matched_raw.get("end_time") or "")
            redpack_id = matched_raw.get("redpack_id") or matched_raw.get("user_redpack_id")
            wm_poi_id = str(sp.get("wm_poi_id") or st_obj.get("wm_poi_id") or "")

            if found_pid:
                promo_params = {
                    "promotion_id": found_pid,
                    "store_id": store_id,
                    "store_name": store_name,
                    "store_icon": order_record.get("store_icon") or "",
                    "platform_code": plat_code,
                    "platform": order_record.get("platform", "meituan"),
                    "end_time": end_time,
                    "redpack_id": redpack_id,
                    "order_money": float(order_record.get("order_money", 0.0)),
                    "rebate_price": float(order_record.get("rebate_money", 0.0)),
                    "rebate_desc": order_record.get("condition") or "",
                    "rebate_type": "percent" if int(order_record.get("order_type") or 0) == 12 else "fixed",
                    "wm_poi_id": wm_poi_id
                }
                order_db_id = order_record.get("id")
                if order_db_id:
                    db.update_order_renew_meta(order_db_id, {
                        "promotion_params": json.dumps(promo_params, ensure_ascii=False)
                    })
                logger.info(f"成功自愈补全订单 #{order_record.get('id')} 的抢单上下文参数: promotion_id={found_pid}")
                return promo_params
    except Exception as e:
        logger.warning(f"订单列表自愈反查失败: {e}")

    # 第二级自愈：根据 store_name / store_id 实时在线检索店铺活动
    if store_name_str:
        try:
            s_res = await client_inst.search_stores(
                keyword=store_name_str,
                city_code=city_code,
                longitude=lon,
                latitude=lat,
                token=token,
                silk_id=silk_id,
                user_id=user_id,
                limit=10
            )
            store_list = s_res.get("store_list") or s_res.get("stores") or []
            matched_store = None
            for s in store_list:
                sid = str(s.get("store_id") or s.get("id") or "")
                sname = str(s.get("name") or s.get("store_name") or "")
                if (store_id_str and sid == store_id_str) or (sname and (sname in store_name_str or store_name_str in sname)):
                    matched_store = s
                    break

            if matched_store:
                promos = matched_store.get("promotions") or []
                chosen_p = promos[0] if promos else None
                if chosen_p and chosen_p.get("promotion_id"):
                    found_pid = str(chosen_p["promotion_id"])
                    plat_code = int(chosen_p.get("store_platform") or matched_store.get("store_platform") or (1 if order_record.get("platform") == "meituan" else 2))
                    promo_params = {
                        "promotion_id": found_pid,
                        "store_id": str(matched_store.get("store_id") or store_id_str),
                        "store_name": str(matched_store.get("name") or store_name_str),
                        "store_icon": order_record.get("store_icon") or matched_store.get("icon") or "",
                        "platform_code": plat_code,
                        "platform": order_record.get("platform", "meituan"),
                        "end_time": str(chosen_p.get("end_time") or ""),
                        "redpack_id": None,
                        "order_money": float(chosen_p.get("order_money", order_record.get("order_money", 0.0))),
                        "rebate_price": float(chosen_p.get("rebate_price", order_record.get("rebate_money", 0.0))),
                        "rebate_desc": chosen_p.get("rebate_desc") or order_record.get("condition") or "",
                        "rebate_type": chosen_p.get("rebate_type") or ("percent" if int(order_record.get("order_type") or 0) == 12 else "fixed"),
                        "wm_poi_id": str(chosen_p.get("wm_poi_id") or matched_store.get("wm_poi_id") or "")
                    }
                    order_db_id = order_record.get("id")
                    if order_db_id:
                        db.update_order_renew_meta(order_db_id, {
                            "promotion_params": json.dumps(promo_params, ensure_ascii=False)
                        })
                    logger.info(f"成功通过商户在线搜索自愈补全订单 #{order_record.get('id')} 的抢单上下文参数: promotion_id={found_pid}")
                    return promo_params
        except Exception as se:
            logger.warning(f"店铺在线搜索自愈失败: {se}")

    return promo_params


async def check_account_meal_ticket_count(
    cli: XiaoCanClient,
    account: Dict[str, Any]
) -> int:
    """
    检查账号当前未使用的可用饭票数量 (status=0)。
    核心安全指标：小蚕规则规定抢单报名必须消耗饭票，若饭票为 0 则无法报名。
    返回可用饭票张数。若网络异常或未能获取返回 -1。
    """
    token = account.get("token")
    silk_id = account.get("silk_id")
    user_id = account.get("user_id")
    city_code = int(account.get("city_code") or 440303)
    if not token:
        return 0
    try:
        res = await cli.get_user_card_list(
            token=token,
            silk_id=silk_id,
            user_id=user_id,
            city_code=city_code,
            status=0,
            offset=0,
            number=100
        )
        card_items = res.get("list") or []
        count = 0
        for it in card_items:
            card = it.get("card") or {}
            cname = str(card.get("name") or "").strip()
            ctype = card.get("card_type")
            if "饭票" in cname or ctype == 0:
                count += 1
        return count
    except Exception as e:
        logger.warning(f"检查账号可用饭票数量异常: {e}")
        return -1


async def execute_order_fast_renew(
    order_record: Dict[str, Any],
    caller_client: Optional[XiaoCanClient] = None,
    action_source: str = "auto_renew"
) -> Dict[str, Any]:
    """
    执行订单极限毫秒级退单与突发重抢续期 (原子执行)
    核心针对用户痛点：
    1. 连接保活与预计算报文，将退单到重抢的时间间隙压缩至 50~150ms；
    2. 若首发未命中库存，以 30ms 极微延迟追加 3 次突发连击，防止被他人截胡；
    3. 成功后重新计算下一次续期；若本次是末次续期，成功后自动终结守护并提醒用户。
    """
    order_id = int(order_record.get("id") or 0)
    promo_order_id = int(order_record.get("promotion_order_id") or 0)
    if not order_id or not promo_order_id:
        return {"ok": False, "message": "无效订单编号"}

    if order_id in _renewing_order_ids:
        return {"ok": False, "message": "该订单正在执行续期重抢中，请勿重复操作"}

    _renewing_order_ids.add(order_id)
    cli = caller_client or client
    log_lines = []
    t_start = time_service.format_now_ms()
    log_lines.append(f"[{t_start}] 启动订单极限毫秒续期守护流程 (原小蚕单号 #{promo_order_id})")

    try:
        # 1. 严格前置安全校验 A: 检查美团端是否已绑定单号
        fresh_order = db.get_order_by_id(order_id) or order_record
        if fresh_order.get("platform_order_id"):
            db.update_order_renew_meta(order_id, {
                "auto_renew": 0,
                "renew_status": "terminated",
                "renew_log": f"[{time_service.format_now_ms()}] 美团端已绑定外卖单号 ({fresh_order.get('platform_order_id')})，续期守护已安全终结"
            })
            return {"ok": False, "message": "订单已在美团下单绑定外卖单号，自动解除续期守护"}

        if fresh_order.get("status") != "pending":
            db.update_order_renew_meta(order_id, {
                "auto_renew": 0,
                "renew_status": "terminated",
                "renew_log": f"[{time_service.format_now_ms()}] 订单状态非待下单 ({fresh_order.get('status')})，续期守护已终结"
            })
            return {"ok": False, "message": f"订单状态已变更为 {fresh_order.get('status')}，无需续期"}

        account_key = fresh_order.get("account_key")
        account = db.get_account_by_key(account_key)
        if not account or not account.get("token"):
            db.update_order_renew_meta(order_id, {
                "renew_status": "failed",
                "renew_log": f"[{time_service.format_now_ms()}] 账号凭据失效或已退出，续期失败"
            })
            return {"ok": False, "message": "账号已失效或退出"}

        token = account["token"]
        silk_id = account.get("silk_id")
        user_id = account.get("user_id")
        city_code = int(account.get("city_code") or 440303)
        lat = str(account.get("latitude") or "22.548361")
        lon = str(account.get("longitude") or "114.13166")

        # 2. 前置安全校验 B: 解析原始抢单参数 (若缺失则启动在线双重自愈补全)
        promo_params = await ensure_order_promotion_params(fresh_order, cli, account)
        promotion_id = promo_params.get("promotion_id")
        if not promotion_id:
            db.update_order_renew_meta(order_id, {
                "renew_status": "failed",
                "renew_log": f"[{time_service.format_now_ms()}] 无法获取该订单关联的活动编号 (promotion_id)，商户活动可能已下架"
            })
            return {"ok": False, "message": "无法自愈获取该订单关联的活动编号，商户活动可能已下架"}

        # 标记为执行中
        db.update_order_renew_meta(order_id, {
            "renew_status": "renewing",
            "renew_log": f"[{time_service.format_now_ms()}] 正在执行极速退单与毫秒重抢..."
        })

        # 3. 校验商户活动是否依然有效 (避免退单后活动已下线导致无法抢回)
        log_lines.append(f"[{time_service.format_now_ms()}] 步骤 1/4: 预检商户活动有效性 (promotion_id={promotion_id})")
        try:
            store_detail = await cli.get_store_promotion_detail(
                promotion_id=promotion_id,
                token=token,
                silk_id=silk_id,
                user_id=user_id,
                city_code=city_code,
                latitude=float(lat),
                longitude=float(lon),
                store_name=str(fresh_order.get("store_name") or "")
            )
            pdetail = store_detail.get("promotion_detail") or store_detail.get("promotion") or {}
            # 官方活动状态说明：
            # 1/2: 进行中; 3: 抢完名额为0 (占座抢满正常现象，退单后将释放); 4/5: 商家永久打烊或活动下架
            # 仅当官方明确返回活动已终结下线 (status=4或5) 时才拦截避让
            status_code = pdetail.get("status")
            if status_code is not None and int(status_code) in (4, 5):
                msg = f"商户活动官方已明确下线结束 (status={status_code})，为保护当前订单名额，严禁退单！"
                log_lines.append(f"[{time_service.format_now_ms()}] {msg}")
                db.update_order_renew_meta(order_id, {
                    "auto_renew": 0,
                    "renew_status": "terminated",
                    "renew_log": "\n".join(log_lines)
                })
                return {"ok": False, "message": msg}
            log_lines.append(f"[{time_service.format_now_ms()}] 步骤 1/4 完成: 商户活动处于有效运营状态 (退单后名额将立即释放回库存池)")
        except Exception as pe:
            logger.warning(f"预检活动状态探测非致命异常: {pe}")
            log_lines.append(f"[{time_service.format_now_ms()}] 步骤 1/4 提示: 活动预检轻微网络抖动 ({pe})，按计划继续执行极速续期")

        # 3.2 核心风控红线预检: 校验账号可用饭票余额
        # 平台硬性规则：饭票数为 0 时无法报名抢单。若贸然取消原单将导致重抢失败而痛失名额，必须安全避让拦截！
        log_lines.append(f"[{time_service.format_now_ms()}] 步骤 1/4 (安全核验): 正在校验账号可用饭票配额...")
        meal_ticket_count = await check_account_meal_ticket_count(cli, account)
        if meal_ticket_count == 0:
            ticket_warn_msg = f"账号【{account.get('nickname')}】可用饭票数量为 0！根据小蚕规则饭票为 0 无法报名抢单。为防止取消后因缺少饭票导致重抢失败痛失名额，系统已安全避让并保留原订单！"
            log_lines.append(f"[{time_service.format_now_ms()}] {ticket_warn_msg}")
            logger.warning(ticket_warn_msg)
            db.update_order_renew_meta(order_id, {
                "auto_renew": 0,
                "renew_status": "insufficient_ticket",
                "renew_log": "\n".join(log_lines)
            })
            await send_system_notification(
                title="霸王餐续期安全避让",
                content=f"账号【{account.get('nickname')}】订单【{fresh_order.get('store_name')}】可用饭票数量为 0，系统已主动拦截退单续期以保护名额，请前往小蚕做任务领取饭票。",
                account_key=account_key,
                event=NotificationEvent.SYSTEM_ALERT,
                level=NotificationLevel.WARNING
            )
            return {"ok": False, "message": "当前账号可用饭票为 0，无法重新抢单。为保护名额已安全拦截退单"}
        elif meal_ticket_count > 0:
            log_lines.append(f"[{time_service.format_now_ms()}] 步骤 1/4 (安全核验通过): 账号持有 {meal_ticket_count} 张可用饭票，具备抢单资格")
        else:
            log_lines.append(f"[{time_service.format_now_ms()}] 步骤 1/4 (安全核验提示): 饭票配额探测轻微网络抖动，按计划推进")

        # 4. 步骤 2: 发起极速退单 (CancelPromotionQuota)
        log_lines.append(f"[{time_service.format_now_ms()}] 步骤 2/4: 发射官方 SilkwormService.CancelPromotionQuota 取消原订单 #{promo_order_id}")
        t_cancel_req = time.time()
        try:
            cancel_res = await cli.cancel_promotion_quota(
                promotion_order_id=promo_order_id,
                token=token,
                silk_id=silk_id,
                user_id=user_id,
                city_code=city_code
            )
        except Exception as ce:
            err_msg = f"调用取消接口异常: {ce}，原订单保持有效，未造成名额损失"
            log_lines.append(f"[{time_service.format_now_ms()}] {err_msg}")
            db.update_order_renew_meta(order_id, {
                "renew_status": "failed",
                "renew_log": "\n".join(log_lines)
            })
            return {"ok": False, "message": err_msg}

        t_cancel_done = time.time()
        cancel_duration_ms = (t_cancel_done - t_cancel_req) * 1000
        log_lines.append(f"[{time_service.format_now_ms()}] 步骤 2/4 完成: 官方退单成功 (耗时 {cancel_duration_ms:.1f}ms)，名额已释放回库存池")

        # 5. 步骤 3: 零延时突发抢回名额
        log_lines.append(f"[{time_service.format_now_ms()}] 步骤 3/4: 零延时直连开火，发起突发毫秒重抢！")
        platform_code = int(promo_params.get("platform_code") or 1)
        redpack_id = promo_params.get("redpack_id")
        grab_ok = False
        new_promo_order_id = 0
        last_grab_msg = ""

        # 连击锁单：最多尝试 3 次，间隔 30ms，消除网络与库存微秒抖动
        for try_idx in range(1, 4):
            t_burst = time_service.format_now_ms()
            try:
                grab_res = await cli.grab_promotion_quota(
                    promotion_id=promotion_id,
                    token=token,
                    silk_id=silk_id,
                    user_id=user_id,
                    city_code=city_code,
                    store_platform=platform_code,
                    if_advance_order=False,
                    latitude=lat,
                    longitude=lon,
                    redpack_id=redpack_id,
                    store_id=str(promo_params.get("store_id") or ""),
                    store_name=str(fresh_order.get("store_name") or ""),
                    wm_poi_id=str(promo_params.get("wm_poi_id") or "")
                )
                ret_order_id = grab_res.get("promotion_order_id") or (grab_res.get("order") or {}).get("promotion_order_id") or 0
                if int(ret_order_id) > 0:
                    new_promo_order_id = int(ret_order_id)
                    grab_ok = True
                    log_lines.append(f"[{t_burst}] 突发连击第 {try_idx} 次命中！成功锁定新订单 #{new_promo_order_id}")
                    break
                else:
                    last_grab_msg = grab_res.get("message") or grab_res.get("msg") or "名额暂未释放"
                    log_lines.append(f"[{t_burst}] 突发第 {try_idx} 次反馈: {last_grab_msg}，立即以 30ms 极微延迟追加连击...")
            except Exception as ge:
                last_grab_msg = str(ge)
                log_lines.append(f"[{t_burst}] 突发第 {try_idx} 次异常: {ge}")
            await asyncio.sleep(0.03)

        # 6. 步骤 4: 结果落盘与状态流转
        now_ts = int(time.time())
        old_renew_count = int(fresh_order.get("renew_count") or 0)
        is_final_was = int(fresh_order.get("is_final_renew") or 0)

        if grab_ok:
            log_lines.append(f"[{time_service.format_now_ms()}] 步骤 4/4: 续期成功，正在落盘新订单并维护续期计划")
            db.update_order(order_id, {
                "status": "cancelled",
                "renew_status": "success",
                "renew_log": "\n".join(log_lines) + f"\n[{time_service.format_now_ms()}] 已由新订单 #{new_promo_order_id} 平滑替换"
            })

            # 计算新订单的续期计划 (遵循用户意图：手动点击极速续期与后台自动守护解耦，严格继承原单的 auto_renew 策略)
            activity_end_time = str(promo_params.get("end_time") or "").strip()
            sched = calculate_order_renew_schedule(order_time=now_ts, activity_end_time_str=activity_end_time, check_now_ts=now_ts)
            was_auto_renew = int(fresh_order.get("auto_renew") or 0) == 1

            if was_auto_renew:
                if is_final_was == 1 or not sched.get("can_renew") or sched.get("is_final") == 1:
                    new_auto_renew = 0
                    new_renew_status = "terminated"
                    new_next_renew_ts = 0
                    new_is_final = 1
                    final_notice_tail = "【活动末次续期已完成】本次已是商户活动结束前最后一次续期，后续请在截止时间前及时消费并在美团下单！"
                else:
                    new_auto_renew = 1
                    new_renew_status = "scheduled"
                    new_next_renew_ts = sched["next_renew_ts"]
                    new_is_final = sched["is_final"]
                    final_notice_tail = f"已重置 2 小时违约倒计时，自动守护中，下次计划续期时刻: {time.strftime('%H:%M:%S', time.localtime(new_next_renew_ts))}"
            else:
                # 原单未开启自动守护：手动点击“极速续期”后，保持未开启自动守护状态，实现手动与自动解耦
                new_auto_renew = 0
                new_renew_status = "idle"
                new_next_renew_ts = 0
                new_is_final = 0
                final_notice_tail = "单次极速续期成功，已重置 2 小时违约倒计时。当前未开启自动守护，如需后台自动续期可随时开启【开续期】。"

            new_exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts + 7200))
            new_order_data = dict(fresh_order)
            new_order_data.pop("id", None)
            new_order_data.update({
                "order_sn": str(new_promo_order_id),
                "promotion_order_id": new_promo_order_id,
                "platform_order_id": "",
                "status": "pending",
                "order_time": now_ts,
                "timeout_time": now_ts + 7200,
                "expire_time": new_exp_str,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
                "auto_renew": new_auto_renew,
                "renew_count": old_renew_count + 1,
                "next_renew_ts": new_next_renew_ts,
                "is_final_renew": new_is_final,
                "renew_status": new_renew_status,
                "renew_log": f"[{time_service.format_now_ms()}] 继自原订单 #{promo_order_id} 极速续期成功 (第 {old_renew_count + 1} 次续期)\n{final_notice_tail}",
                "promotion_params": json.dumps(promo_params, ensure_ascii=False)
            })
            saved_new = db.save_order(new_order_data)

            await send_system_notification(
                title="霸王餐订单自动续期成功",
                content=f"账号【{account.get('nickname')}】店铺【{fresh_order.get('store_name')}】已成功退单并毫秒重抢！新订单号 #{new_promo_order_id}。\n{final_notice_tail}",
                account_key=account_key,
                event=NotificationEvent.GRAB_SUCCESS,
                level=NotificationLevel.SUCCESS
            )
            return {
                "ok": True,
                "message": f"极速续期成功！新单号 #{new_promo_order_id}",
                "new_order_id": new_promo_order_id,
                "order_db_id": saved_new.get("id") if saved_new else None
            }
        else:
            salvage_msg = f"警报: 原订单 #{promo_order_id} 退单后突发重抢未命中 ({last_grab_msg})，系统已自适应挂载 3 秒名额高频捡漏监听引擎！"
            log_lines.append(f"[{time_service.format_now_ms()}] {salvage_msg}")
            logger.error(salvage_msg)

            try:
                db.add_appointment({
                    "account_key": account_key,
                    "store_id": str(fresh_order.get("store_id") or ""),
                    "store_name": str(fresh_order.get("store_name") or ""),
                    "promotion_id": promotion_id,
                    "platform": fresh_order.get("platform", "meituan"),
                    "task_type": "restock",
                    "status": "monitoring",
                    "until_time": promo_params.get("end_time") or "23:59",
                    "check_interval": 3,
                    "order_money": float(fresh_order.get("order_money", 0)),
                    "rebate_price": float(fresh_order.get("rebate_money", 0)),
                    "redpack_id": str(promo_params.get("redpack_id") or "")
                })
                invalidate_worker_cache()
            except Exception as se:
                logger.warning(f"挂载紧急捡漏任务失败: {se}")

            db.update_order(order_id, {
                "renew_status": "failed",
                "renew_log": "\n".join(log_lines)
            })

            await send_system_notification(
                title="霸王餐自动续期未命中预警",
                content=f"账号【{account.get('nickname')}】店铺【{fresh_order.get('store_name')}】退单重抢时名额疑被外部截胡，系统已为您自动启动 3 秒实时捡漏监听！",
                account_key=account_key,
                event=NotificationEvent.SYSTEM_ERROR,
                level=NotificationLevel.ERROR
            )
            return {"ok": False, "message": salvage_msg}

    finally:
        _renewing_order_ids.discard(order_id)


async def process_active_order_renews() -> bool:
    """
    后台常驻巡检订单自动续期任务：
    1. 扫描已到达预定续期时刻的有效订单，毫秒级唤醒并执行原子退单重抢；
    2. 返回是否有 15 秒内即将触发的订单 (用于动态提升调度器主循环精度至 50ms)。
    """
    now_ts = int(time.time())
    due_orders = db.get_renewable_orders(now_ts)
    for ord_row in due_orders:
        oid = ord_row.get("id")
        if oid and oid not in _renewing_order_ids:
            asyncio.create_task(execute_order_fast_renew(ord_row))

    imminent_orders = db.get_imminent_renew_orders(lookahead_seconds=15)
    return len(imminent_orders) > 0


async def _appointment_loop():
    """后台常驻循环：常态 0.5s 低功耗，临近秒杀或整分准点放单时自适应 20ms~50ms 超高精度监测"""
    logger.info("霸王餐预约与名额监听引擎已启动")
    while _is_running:
        try:
            has_primed, has_near_minute = await process_active_appointments()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"预约监听主循环发生未捕获异常: {e}")
            has_primed, has_near_minute = False, False

        has_renew_primed = False
        try:
            has_renew_primed = await process_active_order_renews()
        except asyncio.CancelledError:
            break
        except Exception as re:
            logger.error(f"订单自动续期巡检发生未捕获异常: {re}")
            has_renew_primed = False

        if has_primed:
            await asyncio.sleep(0.02)
        elif has_near_minute or has_renew_primed:
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0.5)
    logger.info("霸王餐预约与名额监听引擎已安全退出")


def start_appointment_worker():
    global _worker_task, _is_running
    if not _is_running:
        _is_running = True
        try:
            loop = asyncio.get_running_loop()
            _worker_task = loop.create_task(_appointment_loop())
        except RuntimeError:
            pass


def stop_appointment_worker():
    global _worker_task, _is_running
    _is_running = False
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
