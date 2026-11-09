import React, { useEffect, useState, useRef } from 'react';
import {
  Card,
  Table,
  Typography,
  Tag,
  Button,
  Toast,
  Tabs,
  TabPane,
  Avatar,
  Input,
  RadioGroup,
  Radio,
  Empty,
  Popconfirm,
  Modal,
  Spin,
  Banner,
  InputNumber,
  Tooltip,
  Switch
} from '@douyinfe/semi-ui';
import { gsap, useGSAP } from '../utils/animations';

import type { ColumnProps } from '@douyinfe/semi-ui/lib/es/table';
import {
  IconRefresh,
  IconCopy,
  IconSearch,
  IconDelete,
  IconGift,
  IconShield,
  IconTicketCode
} from '@douyinfe/semi-icons';
import { useAppStore } from '../store/useAppStore';
import { api } from '../api';
import { useOnActivated } from '../utils/useOnActivated';
import type { Order, DelayCardInfo } from '../types';

const { Title, Text } = Typography;

const getConditionTagColor = (condition?: string): 'green' | 'cyan' | 'blue' | 'amber' | 'grey' => {
  if (!condition) return 'green';
  const c = condition.trim();
  if (c.includes('无需') || c.includes('免评')) {
    return 'green';
  }
  if (c.includes('用餐反馈') || c.includes('反馈')) {
    return 'cyan';
  }
  if (c.includes('随心')) {
    return 'blue';
  }
  if (c.includes('图文') || c.includes('好评') || c.includes('字') || c.includes('图')) {
    return 'amber';
  }
  return 'cyan';
};

const formatRemainingTime = (remainingSec: number | null | undefined): string => {
  if (remainingSec === null || remainingSec === undefined) return '';
  if (remainingSec <= 0) return '已超时';
  const totalMinutes = Math.floor(remainingSec / 60);
  const days = Math.floor(totalMinutes / (60 * 24));
  const hours = Math.floor((totalMinutes % (60 * 24)) / 60);
  const minutes = totalMinutes % 60;

  if (days > 0) {
    return hours > 0 ? `剩 ${days}天${hours}小时` : `剩 ${days}天`;
  }
  if (hours > 0) {
    return minutes > 0 ? `剩 ${hours}小时${minutes}分钟` : `剩 ${hours}小时`;
  }
  return `剩 ${Math.max(1, minutes)}分钟`;
};

const calcRemainingSec = (row?: Order, currentNowSec: number = Math.floor(Date.now() / 1000)): number | null => {
  if (!row) return null;
  let tt = Number(row.timeout_time || 0);
  if (!tt && row.expire_time) {
    const parsed = Date.parse(row.expire_time.replace(/-/g, '/'));
    if (!isNaN(parsed) && parsed > 0) {
      tt = Math.floor(parsed / 1000);
    }
  }
  if (!tt && row.order_time) {
    // 遵循官方 APK 逆向: 基准时效为 8 小时 (28800秒)
    tt = Number(row.order_time) + 28800 + (row.delay_count ? 7200 : 0) + (row.delay_card_used_count ? row.delay_card_used_count * 3600 : 0);
  }
  if (!tt) return null;
  return tt - currentNowSec;
};

const calcPunishRemainingSec = (row?: Order, currentNowSec: number = Math.floor(Date.now() / 1000)): number | null => {
  if (!row || !row.order_time) return null;
  // 小蚕官方 120 分钟 (7200 秒) 扣信用分判定线，以抢单时间为准
  const punishDeadline = Number(row.order_time) + 7200;
  return punishDeadline - currentNowSec;
};

const calcRenewRemainingSec = (row?: Order, currentNowSec: number = Math.floor(Date.now() / 1000)): number | null => {
  if (!row || !row.next_renew_ts || row.next_renew_ts <= 0) return null;
  return Number(row.next_renew_ts) - currentNowSec;
};

export const OrdersView: React.FC = () => {
  const currentAccountKey = useAppStore((s) => s.currentAccountKey);
  const accounts = useAppStore((s) => s.accounts);
  const setActiveTab = useAppStore((s) => s.setActiveTab);
  const containerRef = useRef<HTMLDivElement>(null);
  const tableContainerRef = useRef<HTMLDivElement>(null);
  const [tableScrollY, setTableScrollY] = useState<number>(460);

  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeStatusTab, setActiveStatusTab] = useState<string>('all');
  const [platformFilter, setPlatformFilter] = useState<string>('all');
  const [searchKeyword, setSearchKeyword] = useState<string>('');
  const [delayingOrderId, setDelayingOrderId] = useState<number | null>(null);
  const [delayCardModalVisible, setDelayCardModalVisible] = useState(false);
  const [currentDelayOrder, setCurrentDelayOrder] = useState<Order | null>(null);
  const [delayCardInfo, setDelayCardInfo] = useState<DelayCardInfo | null>(null);
  const [loadingCardInfo, setLoadingCardInfo] = useState(false);
  const [selectedCardCount, setSelectedCardCount] = useState<number>(1);
  const [submittingDelayCard, setSubmittingDelayCard] = useState(false);
  const [cancellingOrderId, setCancellingOrderId] = useState<number | null>(null);
  const [deletingOrderId, setDeletingOrderId] = useState<number | null>(null);
  const [renewingOrderId, setRenewingOrderId] = useState<number | null>(null);
  const [togglingRenewOrderId, setTogglingRenewOrderId] = useState<number | null>(null);
  const [currentNowSec, setCurrentNowSec] = useState<number>(() => Math.floor(Date.now() / 1000));

  // 极速续期选择弹窗状态
  const [renewModalVisible, setRenewModalVisible] = useState(false);
  const [selectedRenewOrder, setSelectedRenewOrder] = useState<Order | null>(null);
  const [renewRedpackMode, setRenewRedpackMode] = useState<number>(2); // 2: 不用红包(占座), 0: 使用最优红包(就餐)
  const [useRebateCard, setUseRebateCard] = useState<boolean>(false); // 是否使用110%返利券(默认不使用，防退单损耗)

  useEffect(() => {
    const timer = setInterval(() => {
      setCurrentNowSec(Math.floor(Date.now() / 1000));
    }, 10000);
    return () => clearInterval(timer);
  }, []);


  // 纯净视口高度自适应：监听表格卡片容器，杜绝 DOM querySelector 与强制同步重排
  useEffect(() => {
    const updateHeight = () => {
      if (!tableContainerRef.current) return;
      const ch = tableContainerRef.current.clientHeight;
      if (ch > 100) {
        // 预留表头(44px)与分页条(48px)及边框空间(约96px)
        const targetH = Math.max(260, ch - 96);
        setTableScrollY((prev) => (Math.abs(prev - targetH) > 12 ? targetH : prev));
      }
    };

    updateHeight();
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined' && tableContainerRef.current) {
      ro = new ResizeObserver(() => {
        updateHeight();
      });
      ro.observe(tableContainerRef.current);
    }

    window.addEventListener('resize', updateHeight);
    return () => {
      window.removeEventListener('resize', updateHeight);
      ro?.disconnect();
    };
  }, []);

  const lastOrdersSignatureRef = useRef<string>('');
  const inFlightOrdersRef = useRef<boolean>(false);

  const fetchOrders = async (kw?: string, force = false, silent = false) => {
    if (accounts.length === 0) {
      setOrders([]);
      return;
    }
    const keywordToSearch = typeof kw === 'string' ? kw.trim() : (searchKeyword || '').trim();
    const effectiveKey = currentAccountKey || accounts[0]?.key || '';
    const queryKey = `${effectiveKey}_${activeStatusTab}_${platformFilter}_${keywordToSearch}`;

    if (!force && lastOrdersSignatureRef.current === queryKey) {
      return;
    }
    if (inFlightOrdersRef.current) return;
    inFlightOrdersRef.current = true;
    lastOrdersSignatureRef.current = queryKey;

    if (!silent) setLoading(true);
    try {
      const ordersRes = await api.getOrders({
        account_key: effectiveKey || undefined,
        status: activeStatusTab,
        platform: platformFilter,
        keyword: keywordToSearch
      });

      if (ordersRes.ok) {
        setOrders(ordersRes.orders || []);
      }
    } catch (e) {
      lastOrdersSignatureRef.current = '';
      Toast.error('拉取订单列表失败');
    } finally {
      inFlightOrdersRef.current = false;
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    fetchOrders();
  }, [currentAccountKey, activeStatusTab, platformFilter]);

  // 页面切入激活或顶级重复点击时立刻拉取最新订单数据
  useOnActivated('orders', () => {
    fetchOrders(undefined, true, false);
  }, { throttleMs: 300 });

  // 筛选与 Tab 切换时的平滑微过渡 (遵循 Semi Design 高性能 GPU 合成通道，杜绝整体表格下坠位移)
  useGSAP(
    () => {
      const tableEl = containerRef.current?.querySelector('.orders-table-container');
      if (tableEl) {
        gsap.killTweensOf(tableEl);
        gsap.fromTo(
          tableEl,
          { autoAlpha: 0.72 },
          {
            autoAlpha: 1,
            duration: 0.18,
            ease: 'power1.out',
            overwrite: 'auto',
            clearProps: 'opacity,visibility',
          }
        );
      }
    },
    { scope: containerRef, dependencies: [activeStatusTab, platformFilter] }
  );

  // 复制文本快捷方法
  const handleCopyText = (txt: string, label: string = '内容') => {
    navigator.clipboard.writeText(txt);
    Toast.success(`${label}已复制到剪贴板`);
  };

  const handleDelayVoucher = async (order: Order) => {
    if (order.platform_order_id) {
      Toast.warning('已绑定外卖单号的订单无法申请延时');
      return;
    }
    setDelayingOrderId(order.id);
    try {
      const res = await api.delayOrderVoucher(order.id);
      if (res.ok) {
        Toast.success(res.message || '官方领红包延时特权已激活，提交凭证有效期已顺延 2 小时');
        fetchOrders(undefined, true);
      } else {
        Toast.error(res.message || '延时申请失败');
      }
    } catch (e: any) {
      Toast.error(`延时操作异常: ${e.response?.data?.detail || e.message || e}`);
    } finally {
      setDelayingOrderId(null);
    }
  };

  const handleOpenDelayCardModal = async (order: Order) => {
    if (order.platform_order_id) {
      Toast.warning('已绑定外卖单号的订单无法使用延时券');
      return;
    }
    setCurrentDelayOrder(order);
    setDelayCardModalVisible(true);
    setLoadingCardInfo(true);
    try {
      const res = await api.getDelayCardInfo(order.id);
      setDelayCardInfo(res);
      const defaultCount = res.max_usable_count > 0 ? 1 : 0;
      setSelectedCardCount(defaultCount);
    } catch (err: any) {
      Toast.error(`获取延时券信息失败: ${err.response?.data?.detail || err.message || err}`);
    } finally {
      setLoadingCardInfo(false);
    }
  };

  const handleConfirmUseDelayCard = async () => {
    if (!currentDelayOrder) return;
    if (selectedCardCount < 1) {
      Toast.warning('请选择至少 1 张延时券');
      return;
    }
    if (delayCardInfo && selectedCardCount > delayCardInfo.max_usable_count) {
      Toast.warning(`使用数量不能超过当前最大允许数量 (${delayCardInfo.max_usable_count} 张)`);
      return;
    }
    setSubmittingDelayCard(true);
    try {
      const res = await api.useDelayCard(currentDelayOrder.id, selectedCardCount);
      if (res.ok) {
        Toast.success(res.message || `成功使用 ${selectedCardCount} 张延时券`);
        setDelayCardModalVisible(false);
        fetchOrders(undefined, true);
      } else {
        Toast.error(res.message || '延时券使用失败');
      }
    } catch (err: any) {
      Toast.error(`延时券使用异常: ${err.response?.data?.detail || err.message || err}`);
    } finally {
      setSubmittingDelayCard(false);
    }
  };

  const handleCancelOrder = async (order: Order) => {
    setCancellingOrderId(order.id);
    try {
      const res = await api.cancelOrder(order.id);
      if (res.ok) {
        Toast.success(res.message || '霸王餐报名已成功取消');
        fetchOrders(undefined, true);
      } else {
        Toast.error(res.message || '取消报名失败');
      }
    } catch (e: any) {
      Toast.error(`取消报名异常: ${e.response?.data?.detail || e.message || e}`);
    } finally {
      setCancellingOrderId(null);
    }
  };

  const handleDeleteOrder = async (order: Order) => {
    setDeletingOrderId(order.id);
    try {
      const res = await api.deleteOrder(order.id);
      if (res.ok) {
        Toast.success(res.message || '订单记录已删除');
        fetchOrders(undefined, true);
      } else {
        Toast.error(res.message || '删除订单失败');
      }
    } catch (e: any) {
      Toast.error(`删除订单异常: ${e.response?.data?.detail || e.message || e}`);
    } finally {
      setDeletingOrderId(null);
    }
  };

  const handleToggleAutoRenew = async (order: Order) => {
    setTogglingRenewOrderId(order.id);
    try {
      const willEnable = !(order.auto_renew === 1);
      const res = await api.toggleOrderAutoRenew(order.id, willEnable);
      if (res.ok) {
        Toast.success(res.message || (willEnable ? '已开启自动无损续期守护' : '已关闭自动续期守护'));
        // 乐观同步界面状态，杜绝等待与闪烁
        setOrders(prev => prev.map(o => o.id === order.id ? {
          ...o,
          auto_renew: willEnable ? 1 : 0,
          next_renew_ts: res.next_renew_ts !== undefined ? res.next_renew_ts : (willEnable ? o.next_renew_ts : 0),
          is_final_renew: res.is_final !== undefined ? res.is_final : o.is_final_renew,
          renew_status: willEnable ? 'scheduled' : 'idle'
        } : o));
        fetchOrders(undefined, true, true);
      } else {
        Toast.error(res.message || '操作失败');
      }
    } catch (e: any) {
      Toast.error(`切换续期失败: ${e.response?.data?.detail || e.message || e}`);
    } finally {
      setTogglingRenewOrderId(null);
    }
  };

  const handleOpenRenewModal = (order: Order) => {
    setSelectedRenewOrder(order);
    setRenewRedpackMode(2); // 默认不使用红包，保障占座安全
    setUseRebateCard(false); // 默认不使用110%返利券，防止退单作废
    setRenewModalVisible(true);
  };

  const handleConfirmRenew = async () => {
    if (!selectedRenewOrder) return;
    const order = selectedRenewOrder;
    setRenewingOrderId(order.id);
    try {
      const res = await api.executeOrderRenewNow(order.id, {
        redpack_mode: renewRedpackMode,
        use_rebate_card: useRebateCard
      });
      if (res.ok) {
        Toast.success(res.message || '极速续期成功，已重置2小时免罚倒计时');
        setRenewModalVisible(false);
        fetchOrders(undefined, true);
      } else {
        Toast.error(res.message || '极速续期失败');
      }
    } catch (e: any) {
      Toast.error(`极速续期异常: ${e.response?.data?.detail || e.message || e}`);
    } finally {
      setRenewingOrderId(null);
    }
  };


  const DEFAULT_PLATFORM_ICONS: Record<string, string> = {
    jingdong: 'https://img10.360buyimg.com/imagetools/jfs/t1/282159/28/8442/32950/67e10a0dFb8e53ae1/3a4d405ef2b69aff.jpg',
    jd: 'https://img10.360buyimg.com/imagetools/jfs/t1/282159/28/8442/32950/67e10a0dFb8e53ae1/3a4d405ef2b69aff.jpg',
    eleme: 'https://cube.elemecdn.com/8/FF/D6D23A46B485F85152E1F9A855510jpg.jpg',
    taobao: 'https://cube.elemecdn.com/8/FF/D6D23A46B485F85152E1F9A855510jpg.jpg',
    meituan: 'http://p0.meituan.net/business/cf047871536dfa726936dfdc834b23c7211376.jpg'
  };

  const getPlatformBadge = (plat?: string) => {
    if (plat === 'jingdong' || plat === 'jd') {
      return { label: '京东外卖', color: 'red' as const, avatarColor: 'red' as const };
    }
    if (plat === 'eleme' || plat === 'taobao') {
      return { label: '饿了么', color: 'blue' as const, avatarColor: 'blue' as const };
    }
    return { label: '美团外卖', color: 'orange' as const, avatarColor: 'orange' as const };
  };

  const columns: ColumnProps<Order>[] = [
    {
      title: '店铺与小蚕单号',
      dataIndex: 'store_name',
      width: 320,
      render: (name: string, row?: Order) => {
        const platInfo = getPlatformBadge(row?.platform);
        const avatarSrc = row?.store_icon || DEFAULT_PLATFORM_ICONS[row?.platform || 'meituan'];
        return (
          <div className="flex items-start gap-3 py-1 max-w-[320px]">
            <Avatar
              size="medium"
              shape="square"
              src={avatarSrc}
              color={platInfo.avatarColor}
              className="flex-shrink-0 mt-0.5 rounded-md"
            >
              {name?.[0] || '外'}
            </Avatar>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <Tag color={platInfo.color} size="small" shape="square" className="flex-shrink-0">
                  {platInfo.label}
                </Tag>
                <Typography.Text
                  ellipsis={{ showTooltip: true }}
                  strong
                  className="text-sm font-semibold text-semi-color-text-0 hover:text-semi-color-primary transition-colors cursor-pointer"
                  title="点击复制店铺名"
                  onClick={() => handleCopyText(name, '店铺名称')}
                >
                  {name}
                </Typography.Text>
              </div>
              <div className="flex items-center gap-1.5 mt-1 text-xs text-semi-color-text-2 font-mono">
                <span className="truncate max-w-[190px]">单号: {row?.order_sn}</span>
                <Button
                  theme="borderless"
                  icon={<IconCopy size="small" />}
                  size="small"
                  className="p-0.5 h-4 w-4 flex-shrink-0 text-semi-color-text-2 hover:text-semi-color-primary"
                  onClick={() => row && handleCopyText(row.order_sn, '小蚕单号')}
                />
              </div>
            </div>
          </div>
        );
      },
    },
    {
      title: '实付与返利',
      dataIndex: 'order_money',
      width: 180,
      render: (_: any, row?: Order) => {
        const hasRedpack = Boolean(row?.redpack_reward_num && row.redpack_reward_num > 0);
        const redpackAmount = hasRedpack
          ? ((row!.redpack_reward_num!) > 5 ? (row!.redpack_reward_num!) / 100 : (row!.redpack_reward_num!)).toFixed(2)
          : null;
        const isPercentOrder =
          row?.order_type === 12 ||
          Boolean(row?.rebate_ratio && row.rebate_ratio > 0) ||
          Number(row?.order_money || 0) <= 0;
        const hasRealOrderMoney = Number(row?.order_money || 0) > 0;
        return (
          <div className="space-y-1 py-1">
            <div className="flex items-baseline gap-1.5">
              <span className="text-base font-bold text-semi-color-danger font-mono tracking-tight">
                ¥{row?.rebate_money}
              </span>
              {redpackAmount && (
                <span className="text-xs font-semibold text-semi-color-warning font-mono">
                  +¥{redpackAmount}红包
                </span>
              )}
            </div>
            <div className="text-sm text-semi-color-text-1 font-mono">
              {isPercentOrder && !hasRealOrderMoney ? (
                <span>按比例返: {row?.rebate_ratio ? `${row.rebate_ratio}%` : '比例返'}</span>
              ) : (
                <span>门槛实付: ¥{row?.order_money}</span>
              )}
            </div>
          </div>
        );
      },
    },
    {
      title: '外卖单号',
      dataIndex: 'platform_order_id',
      width: 200,
      render: (platOrderId: string) => {
        const hasExtId = !!platOrderId;
        if (!hasExtId) {
          return (
            <span className="text-sm text-semi-color-text-0">
              未绑定单号
            </span>
          );
        }
        return (
          <div className="flex items-center gap-1.5 py-1">
            <span className="text-sm font-mono text-semi-color-text-0 truncate max-w-[170px]" title={platOrderId}>
              {platOrderId}
            </span>
            <Button
              theme="borderless"
              icon={<IconCopy size="small" />}
              size="small"
              className="p-0.5 h-4 w-4 flex-shrink-0 text-semi-color-text-2 hover:text-semi-color-primary"
              onClick={() => handleCopyText(platOrderId, '外卖单号')}
            />
          </div>
        );
      },
    },
    {
      title: '剩余时间',
      dataIndex: 'timeout_time',
      width: 260,
      render: (_: any, row?: Order) => {
        if (!row) return null;
        const status = row.status;
        const hasExtId = !!row.platform_order_id;
        const remainingSec = calcRemainingSec(row, currentNowSec);
        const countdownStr = formatRemainingTime(remainingSec);
        const hasDelayed = Boolean(
          (row.delay_count && row.delay_count > 0) ||
          (row.timeout_time && row.order_time && (row.timeout_time - row.order_time) >= 25200)
        );
        const cardUsed = Number(row.delay_card_used_count || 0);

        if (status === 'pending') {
          if (countdownStr === '已超时' || (remainingSec !== null && remainingSec <= 0)) {
            return (
              <span className="text-sm text-semi-color-text-0 font-mono">
                已超时关闭
              </span>
            );
          }
          const isUrgent = remainingSec !== null && remainingSec < 1800;
          const punishSec = !hasExtId ? calcPunishRemainingSec(row, currentNowSec) : null;
          const renewSec = (!hasExtId && row.auto_renew === 1) ? calcRenewRemainingSec(row, currentNowSec) : null;

          return (
            <div className="space-y-1 py-1">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className={`text-sm font-semibold font-mono ${isUrgent ? 'text-semi-color-danger' : 'text-semi-color-warning'}`}>
                  凭据倒计时: {countdownStr}
                </span>
                {hasDelayed && (
                  <Tag color="violet" size="small" shape="square" className="!px-1 !py-0 text-xs">
                    延时2h
                  </Tag>
                )}
                {cardUsed > 0 && (
                  <Tag color="amber" size="small" shape="square" className="!px-1 !py-0 text-xs">
                    已用券{cardUsed}张
                  </Tag>
                )}
              </div>

              {!hasExtId && (
                <div className="text-xs font-mono flex items-center gap-1.5 flex-wrap">
                  {punishSec !== null && (
                    punishSec > 0 ? (
                      <span className="text-semi-color-text-2">
                        免罚退单: 剩 {formatRemainingTime(punishSec)}
                      </span>
                    ) : (
                      <span className="text-semi-color-danger font-semibold">
                        已超2小时(退单将扣分)
                      </span>
                    )
                  )}

                  {row.is_activity_expired ? (
                    <Tooltip
                      content={`商户活动报名已于 ${row.activity_end_time || '此前'} 截止，此时退单无法重新抢回名额，已自动安全锁定`}
                      position="top"
                    >
                      <Tag color="orange" size="small" shape="square" className="!px-1 !py-0 text-xs cursor-pointer">
                        活动已截止
                      </Tag>
                    </Tooltip>
                  ) : row.auto_renew === 1 ? (
                    <Tooltip
                      content={row.renew_log || (row.is_final_renew === 1 ? '活动结束前5分钟末次退单重抢' : '下单后110分钟自动退单重抢以重置2小时限制')}
                      position="top"
                    >
                      <Tag color={row.is_final_renew === 1 ? 'orange' : 'green'} size="small" shape="square" className="!px-1 !py-0 text-xs cursor-pointer">
                        {row.is_final_renew === 1 ? '末次续期' : '续期守护'}
                        {renewSec !== null ? (renewSec > 0 ? ` (${formatRemainingTime(renewSec)})` : ' (就绪)') : ''}
                        {row.renew_count ? ` · 第${row.renew_count}次` : ''}
                      </Tag>
                    </Tooltip>
                  ) : row.renew_status === 'insufficient_ticket' ? (
                    <Tooltip content={row.renew_log || '账号可用饭票为0，系统已主动拦截退单以保护名额'} position="top">
                      <Tag color="red" size="small" shape="square" className="!px-1 !py-0 text-xs cursor-pointer">
                        饭票不足避让
                      </Tag>
                    </Tooltip>
                  ) : (
                    <Tag color="grey" size="small" shape="square" className="!px-1 !py-0 text-xs">
                      未开续期守护
                    </Tag>
                  )}
                </div>
              )}

              <div className="text-xs text-semi-color-text-2 font-mono">
                <span>截止 {row.expire_time ? row.expire_time.slice(5, 16) : '-'}</span>
              </div>
            </div>
          );
        }
        if (status === 'completed') {
          if (!hasExtId) {
            return <span className="text-sm text-semi-color-text-0">已关闭</span>;
          }
          return <span className="text-sm text-semi-color-text-2">-</span>;
        }
        if (status === 'cancelled') {
          return <span className="text-sm text-semi-color-text-0">已关闭</span>;
        }
        if (status === 'auditing') {
          return <span className="text-sm text-semi-color-warning">审核中</span>;
        }
        if (status === 'rejected') {
          return <span className="text-sm text-semi-color-danger">已驳回</span>;
        }
        return <span className="text-sm text-semi-color-text-2">-</span>;
      },
    },
    {
      title: '订单状态',
      dataIndex: 'status',
      width: 120,
      render: (status: string, row?: Order) => {
        const hasExtId = !!row?.platform_order_id;
        if (status === 'completed') {
          if (!hasExtId) {
            return (
              <Tag color="grey" size="small" shape="square">
                已取消
              </Tag>
            );
          }
          return (
            <Tag color="green" size="small" shape="square">
              已到账
            </Tag>
          );
        }
        if (status === 'auditing') {
          return (
            <Tag color="orange" size="small" shape="square">
              审核中
            </Tag>
          );
        }
        if (status === 'pending') {
          if (hasExtId) {
            return (
              <Tag color="cyan" size="small" shape="square">
                已下单·待反馈
              </Tag>
            );
          }
          return (
            <Tag color="blue" size="small" shape="square">
              待下单
            </Tag>
          );
        }
        if (status === 'rejected') {
          return (
            <Tooltip content={row?.reject_reason || '平台已驳回'} position="top">
              <Tag color="red" size="small" shape="square">
                已驳回
              </Tag>
            </Tooltip>
          );
        }
        if (status === 'cancelled') {
          return (
            <Tag color="grey" size="small" shape="square">
              已取消
            </Tag>
          );
        }
        return <Tag color="grey" size="small" shape="square">{status}</Tag>;
      },
    },
    {
      title: '评价要求',
      dataIndex: 'condition',
      width: 140,
      render: (cond: string) => {
        const text = cond || '无需评价';
        return (
          <Tag color={getConditionTagColor(text)} size="small" shape="square">
            {text}
          </Tag>
        );
      },
    },
    {
      title: '下单时间',
      dataIndex: 'created_at',
      width: 140,
      render: (timeStr: string) => (
        <span className="text-sm text-semi-color-text-2 font-mono">
          {timeStr ? timeStr.slice(5, 16) : '-'}
        </span>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 320,
      fixed: 'right',
      render: (_: any, row?: Order) => {
        if (!row) return null;
        const status = row.status;
        const hasExtId = !!row.platform_order_id;
        const isPercentOrder =
          row.order_type === 12 ||
          Boolean(row.rebate_ratio && row.rebate_ratio > 0) ||
          Number(row.order_money || 0) <= 0;
        const hasDelayed = Boolean(
          (row.delay_count && row.delay_count > 0) ||
          (row.timeout_time && row.order_time && (row.timeout_time - row.order_time) >= 25200)
        );

        if (status === 'pending') {
          return (
            <div className="flex items-center gap-1.5 flex-wrap">
              {!hasExtId && (
                <>
                  <Button
                    theme="light"
                    type="primary"
                    size="small"
                    disabled={Boolean(row.is_activity_expired)}
                    loading={renewingOrderId === row.id}
                    onClick={() => handleOpenRenewModal(row)}
                    title={row.is_activity_expired ? '商户活动报名已截止，无法执行退单重抢' : '极速退单并突发重抢以重置2小时限制，支持自由选择是否使用红包'}
                  >
                    极速续期
                  </Button>

                  <Button
                    theme="light"
                    type={row.auto_renew === 1 ? 'tertiary' : 'secondary'}
                    size="small"
                    disabled={Boolean(row.is_activity_expired && row.auto_renew !== 1)}
                    loading={togglingRenewOrderId === row.id}
                    onClick={() => handleToggleAutoRenew(row)}
                    title={row.is_activity_expired ? '商户活动报名已截止，无法开启续期守护' : (row.auto_renew === 1 ? '点击关闭 110 分钟自动续期守护' : '点击开启 110 分钟自动退单重抢守护')}
                  >
                    {row.auto_renew === 1 ? '关续期' : '开续期'}
                  </Button>
                </>
              )}

              {!isPercentOrder && !hasExtId && (
                <>
                  {!hasDelayed ? (
                    <Button
                      theme="light"
                      type="warning"
                      size="small"
                      loading={delayingOrderId === row.id}
                      onClick={() => handleDelayVoucher(row)}
                      title="领红包免费延长有效期 2 小时"
                    >
                      延时2h
                    </Button>
                  ) : (
                    <Button
                      theme="light"
                      type="warning"
                      size="small"
                      onClick={() => handleOpenDelayCardModal(row)}
                      title="使用卡包内的延时券延长提交时间"
                    >
                      延时券
                    </Button>
                  )}
                </>
              )}
              {!hasExtId ? (
                <Popconfirm
                  title="确认取消该霸王餐报名？"
                  content="取消后将释放当前占用的抢单名额，该操作不可撤销。"
                  okText="确认取消"
                  cancelText="返回"
                  okType="danger"
                  onConfirm={() => handleCancelOrder(row)}
                >
                  <Button
                    theme="light"
                    type="danger"
                    size="small"
                    loading={cancellingOrderId === row.id}
                  >
                    取消
                  </Button>
                </Popconfirm>
              ) : (
                <span className="text-sm text-semi-color-text-2">-</span>
              )}
            </div>
          );
        }

        if (status === 'completed' || status === 'cancelled') {
          return (
            <Popconfirm
              title="确认删除该订单？"
              content="将从官方订单列表与本地记录中彻底删除该条订单。"
              okText="确认删除"
              cancelText="取消"
              okType="danger"
              onConfirm={() => handleDeleteOrder(row)}
            >
              <Button
                theme="light"
                type="tertiary"
                size="small"
                icon={<IconDelete size="small" />}
                loading={deletingOrderId === row.id}
              >
                删除订单
              </Button>
            </Popconfirm>
          );
        }

        if (status === 'auditing') {
          return <span className="text-xs text-semi-color-text-2">平台审核中</span>;
        }

        if (status === 'rejected') {
          return <span className="text-xs text-semi-color-danger">已驳回</span>;
        }

        return <span className="text-xs text-semi-color-text-3">-</span>;
      },
    }
  ];

  if (accounts.length === 0) {
    return (
      <div className="w-full flex flex-col h-full min-h-0 space-y-3">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 shrink-0">
          <div>
            <Title heading={3}>我的霸王餐订单与返利</Title>
            <Text type="secondary" size="small">实时跟踪已抢霸王餐状态与返利到账明细</Text>
          </div>
        </div>
        <Card className="flex-1 min-h-0 rounded-xl border border-semi-color-border py-16 flex flex-col items-center justify-center text-center bg-semi-color-bg-0">
          <Empty
            title="当前未登录或未托管小蚕账号"
            description="查看已抢霸王餐订单与返利明细需要真实小蚕账号凭证。当前已退出或无登录账号，系统已停止拉取任何订单数据。"
          >
            <Button
              theme="solid"
              type="primary"
              onClick={() => setActiveTab('accounts')}
              className="mt-4"
            >
              前往「小蚕账号」添加或绑定账号
            </Button>
          </Empty>
        </Card>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="w-full flex flex-col h-full min-h-0 space-y-3">
      {/* 注入表格高度自适应与分页规范样式 */}
      <style>{`
        .orders-table-container .semi-table-body {
          height: var(--table-scroll-y) !important;
          min-height: var(--table-scroll-y) !important;
          max-height: var(--table-scroll-y) !important;
          overflow-y: auto !important;
          contain: content;
          will-change: transform;
          transform: translateZ(0);
          -webkit-overflow-scrolling: touch;
        }
        .orders-table-container .semi-table-placeholder {
          min-height: var(--table-scroll-y) !important;
          display: flex !important;
          align-items: center !important;
          justify-content: center !important;
        }
        .orders-table-container .semi-spin-wrapper {
          display: flex !important;
          flex-direction: column !important;
          align-items: center !important;
          justify-content: center !important;
          position: absolute !important;
          top: 50% !important;
          left: 50% !important;
          transform: translate(-50%, -50%) !important;
          width: auto !important;
          height: auto !important;
          z-index: 10 !important;
        }
        .orders-table-container .semi-table-pagination-outer {
          padding: 10px 16px !important;
          margin: 0 !important;
          margin-top: auto !important;
          flex-shrink: 0 !important;
          border-top: 1px solid var(--semi-color-border) !important;
          display: flex !important;
          align-items: center !important;
          justify-content: space-between !important;
          background-color: var(--semi-color-bg-0) !important;
        }
      `}</style>

      {/* 头部标题与操作 */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 shrink-0">
        <div>
          <Title heading={3}>我的霸王餐订单与返利</Title>
          <Text type="secondary" size="small">实时跟踪已抢霸王餐状态与返利到账明细</Text>
        </div>

        <Button
          icon={<IconRefresh spin={loading} />}
          loading={loading}
          onClick={() => fetchOrders(undefined, true)}
        >
          刷新订单
        </Button>
      </div>

      {/* 状态分类与平台过滤控制栏 */}
      <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shrink-0">
        <Tabs
          type="button"
          activeKey={activeStatusTab}
          onChange={(k) => setActiveStatusTab(String(k))}
          className="shrink-0"
        >
          <TabPane tab="全部" itemKey="all" />
          <TabPane tab="待上传" itemKey="pending" />
          <TabPane tab="审核中" itemKey="auditing" />
          <TabPane tab="已完成" itemKey="completed" />
          <TabPane tab="已驳回" itemKey="rejected" />
          <TabPane tab="已取消" itemKey="cancelled" />
        </Tabs>

        <div className="flex items-center gap-3 flex-wrap sm:flex-nowrap">
          <RadioGroup
            type="button"
            value={platformFilter}
            onChange={(e) => setPlatformFilter(e.target.value)}
          >
            <Radio value="all">全部</Radio>
            <Radio value="meituan">美团</Radio>
            <Radio value="eleme">饿了么</Radio>
            <Radio value="jingdong">京东</Radio>
          </RadioGroup>

          <Input
            prefix={<IconSearch />}
            placeholder="搜索店铺名、单号..."
            value={searchKeyword}
            onChange={(val) => setSearchKeyword(val || '')}
            onEnterPress={() => fetchOrders()}
            showClear
            onClear={() => {
              setSearchKeyword('');
              fetchOrders('');
            }}
            style={{ width: 220 }}
          />
        </div>
      </div>

      {/* 主体表格卡片：撑满视口垂直剩余高度，杜绝超出页面滚动 */}
      <Card
        className="flex-1 min-h-0 flex flex-col rounded-xl border border-semi-color-border shadow-xs overflow-hidden bg-semi-color-bg-0"
        bodyStyle={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, padding: 0 }}
      >
        <div
          ref={tableContainerRef}
          className="orders-table-container relative flex-1 min-h-0 flex flex-col overflow-hidden"
          style={{
            ['--table-scroll-y' as any]: `${tableScrollY}px`
          }}
        >
          <Table
            rowKey="id"
            columns={columns}
            dataSource={orders}
            loading={loading}
            pagination={{
              pageSize: 20,
              showSizeChanger: true,
              pageSizeOpts: [10, 20, 50, 100],
            }}
            scroll={{ y: tableScrollY, x: 1200 }}
            size="middle"
            empty={
              loading ? (
                <div className="py-20 text-center flex flex-col items-center justify-center m-auto">
                  <div className="text-sm font-medium text-semi-color-text-0 mt-8">
                    正在同步霸王餐订单记录...
                  </div>
                </div>
              ) : (
                <div className="py-16 text-center flex flex-col items-center justify-center m-auto">
                  <Empty
                    title="暂无匹配的霸王餐订单记录"
                    description="已抢到的霸王餐订单将自动同步展示于此，支持返利明细与审核进度实时追踪"
                  />
                </div>
              )
            }
          />
        </div>
      </Card>

      {/* 延时券二次确认弹窗 (标准 Semi Design 规范) */}
      <Modal
        title="使用延时券"
        visible={delayCardModalVisible}
        onCancel={() => setDelayCardModalVisible(false)}
        onOk={handleConfirmUseDelayCard}
        okText="确定使用"
        cancelText="取消"
        okType="warning"
        confirmLoading={submittingDelayCard}
        okButtonProps={{
          disabled: !delayCardInfo || delayCardInfo.max_usable_count <= 0
        }}
        width={560}
        centered
      >
        {loadingCardInfo ? (
          <div className="py-12 flex flex-col items-center justify-center gap-3">
            <Spin size="large" />
            <Text type="secondary" size="small">正在获取卡包延时资产...</Text>
          </div>
        ) : (
          <div className="space-y-4 pt-1">
            <Banner
              type="info"
              fullMode={false}
              description={
                <div>
                  <div className="font-medium text-semi-color-text-0">
                    延时券单张可延长 1 小时提交订单时间
                  </div>
                  <div className="text-xs text-semi-color-text-2 mt-0.5">
                    根据平台规则，使用延时券最迟可延长到次日早上 6 点整。
                  </div>
                </div>
              }
            />

            {/* 订单与卡包资产概览 */}
            <div className="bg-semi-color-fill-0 border border-semi-color-border rounded-lg p-3 space-y-2">
              <div className="flex justify-between items-start text-sm gap-3">
                <Text type="secondary" className="whitespace-nowrap flex-shrink-0 pt-0.5">所属商户</Text>
                <Text strong className="text-right text-semi-color-text-0 break-words flex-1 font-semibold leading-snug">
                  {currentDelayOrder?.store_name || '-'}
                </Text>
              </div>
              <div className="flex justify-between items-center text-sm">
                <Text type="secondary">当前提交截止时间</Text>
                <Text className="font-mono">{delayCardInfo?.expire_time || '-'}</Text>
              </div>
              <div className="flex justify-between items-center text-sm">
                <Text type="secondary">次日 06:00 截止上限</Text>
                <Tag color="grey" size="small">{delayCardInfo?.next_6am_str || '-'}</Tag>
              </div>
              <div className="flex justify-between items-center text-sm pt-1 border-t border-semi-color-border">
                <Text type="secondary">卡包可用延时券</Text>
                <div>
                  <Text strong className="text-semi-color-warning text-base font-bold font-mono">
                    {delayCardInfo?.available_delay_cards ?? 0}
                  </Text>
                  <Text type="secondary" size="small"> 张</Text>
                </div>
              </div>
            </div>

            {/* 使用数量选择区 */}
            <div className="border border-semi-color-border rounded-lg p-3.5 space-y-3">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-semibold text-semi-color-text-0">选择使用延时券数量</div>
                  <div className="text-xs text-semi-color-text-2 mt-0.5">
                    当前最多允许使用 {delayCardInfo?.max_usable_count ?? 0} 张
                  </div>
                </div>

                <InputNumber
                  min={1}
                  max={Math.max(1, delayCardInfo?.max_usable_count || 1)}
                  value={selectedCardCount}
                  onChange={(val) => {
                    const maxVal = delayCardInfo?.max_usable_count || 1;
                    const num = typeof val === 'number' ? Math.max(1, Math.min(val, maxVal)) : 1;
                    setSelectedCardCount(num);
                  }}
                  style={{ width: 140 }}
                  size="default"
                  disabled={!delayCardInfo || delayCardInfo.max_usable_count <= 0}
                  suffix="张"
                />
              </div>

              {/* 预计延长结果预览 */}
              {delayCardInfo && delayCardInfo.max_usable_count > 0 && (
                <div className="text-xs bg-semi-color-fill-1 rounded p-2 flex justify-between items-center">
                  <Text type="secondary">使用后预计截止时间:</Text>
                  <Text strong className="text-semi-color-primary font-mono">
                    {new Date((delayCardInfo.timeout_time + selectedCardCount * 3600) * 1000).toLocaleString('zh-CN', { hour12: false })}
                  </Text>
                </div>
              )}
            </div>

            {delayCardInfo && delayCardInfo.max_usable_count <= 0 && (
              <Banner
                type="warning"
                fullMode={false}
                description={
                  delayCardInfo.available_delay_cards <= 0
                    ? '当前卡包内暂无可用延时券，请先在官方 App 领取或兑换延时券。'
                    : '当前订单超时时间已逼近次日 06:00 阈值，已达到最迟延时上限，无法继续使用延时券。'
                }
              />
            )}
          </div>
        )}
      </Modal>

      {/* 极速续期与就餐红包策略选择弹窗 */}
      <Modal
        title={
          <div className="flex items-center gap-2">
            <IconRefresh className="text-semi-color-primary" />
            <span>极速续期与就餐红包选择</span>
          </div>
        }
        visible={renewModalVisible}
        onCancel={() => {
          if (!renewingOrderId) setRenewModalVisible(false);
        }}
        footer={
          <div className="flex items-center justify-between w-full">
            <span className="text-xs text-semi-color-text-2">
              {renewRedpackMode === 0 ? '将自动锁定最高面额红包下单' : '零红包重抢，防退单红包作废'}
              {useRebateCard ? ' · 已开启110%返利券' : ''}
            </span>
            <div className="flex items-center gap-2">
              <Button
                disabled={Boolean(renewingOrderId)}
                onClick={() => setRenewModalVisible(false)}
              >
                取消
              </Button>
              <Button
                theme="solid"
                type="primary"
                loading={Boolean(renewingOrderId)}
                onClick={handleConfirmRenew}
              >
                立即确认续期
              </Button>
            </div>
          </div>
        }
        width={520}
        centered
      >
        {selectedRenewOrder && (
          <div className="space-y-4 py-1">
            <div className="p-3 bg-semi-color-fill-0 border border-semi-color-border rounded-lg space-y-1">
              <div className="flex items-center justify-between">
                <Text strong className="text-sm text-semi-color-text-0">
                  {selectedRenewOrder.store_name}
                </Text>
                <Tag color="cyan" size="small">
                  原单号 #{selectedRenewOrder.promotion_order_id || selectedRenewOrder.order_sn}
                </Tag>
              </div>
              <div className="text-xs text-semi-color-text-2 flex items-center gap-3">
                <span>实付: ¥{selectedRenewOrder.order_money}</span>
                <span>返现: ¥{selectedRenewOrder.rebate_money}</span>
                <span>状态: 待下单中</span>
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Text strong className="text-xs text-semi-color-text-1 uppercase tracking-wider">
                  本次续期重抢红包策略
                </Text>
                <Tag color={renewRedpackMode === 0 ? 'red' : 'blue'} size="small">
                  {renewRedpackMode === 0 ? '就餐抵扣模式' : '安全占座模式'}
                </Tag>
              </div>

              <RadioGroup
                direction="vertical"
                value={renewRedpackMode}
                onChange={(e) => setRenewRedpackMode(Number(e.target.value))}
                className="w-full space-y-2"
              >
                <Radio
                  value={2}
                  className={`w-full !p-3 !m-0 rounded-lg border transition-all ${
                    renewRedpackMode === 2
                      ? 'border-semi-color-primary bg-semi-color-primary-light-default'
                      : 'border-semi-color-border hover:bg-semi-color-fill-0'
                  }`}
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-1.5">
                      <IconShield size="small" className="text-semi-color-primary" />
                      <span className="font-semibold text-sm text-semi-color-text-0">
                        不使用红包 (推荐 · 继续占座)
                      </span>
                      <Tag color="green" size="small">防作废安全</Tag>
                    </div>
                    <div className="text-xs text-semi-color-text-2 leading-relaxed">
                      适合人未到店、仅刷新 2 小时就餐倒计时的场景。零红包重抢，彻底防止因超过 1 小时退单造成红包永久作废。
                    </div>
                  </div>
                </Radio>

                <Radio
                  value={0}
                  className={`w-full !p-3 !m-0 rounded-lg border transition-all ${
                    renewRedpackMode === 0
                      ? 'border-red-500 bg-red-50/60 dark:bg-red-950/20'
                      : 'border-semi-color-border hover:bg-semi-color-fill-0'
                  }`}
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-1.5">
                      <IconGift size="small" className="text-red-500" />
                      <span className="font-semibold text-sm text-semi-color-text-0">
                        使用最优红包 (确定就餐 · 享受立减)
                      </span>
                      <Tag color="red" size="small">即抢即吃</Tag>
                    </div>
                    <div className="text-xs text-semi-color-text-2 leading-relaxed">
                      适合马上前往美团/饿了么下单就餐的场景。系统在退单前自动匹配锁定当前账号内额度最高的外卖红包，并在毫秒突发重抢时带上该红包下单锁定，抢到后请尽快在端内就餐消费。
                    </div>
                  </div>
                </Radio>
              </RadioGroup>
            </div>

            {/* 110% 返利加成券配置卡片 */}
            <div className={`p-3 rounded-lg border transition-all ${useRebateCard ? 'border-amber-500 bg-amber-50/60 dark:bg-amber-950/20' : 'border-semi-color-border bg-semi-color-fill-0'}`}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <IconTicketCode className={useRebateCard ? 'text-amber-500' : 'text-semi-color-text-2'} />
                  <span className="font-semibold text-sm text-semi-color-text-0">使用 110% 返利券 (加成卡)</span>
                  <Tag color={useRebateCard ? 'amber' : 'grey'} size="small">
                    {useRebateCard ? '确定就餐使用' : '默认不使用'}
                  </Tag>
                </div>
                <Switch
                  checked={useRebateCard}
                  onChange={(checked) => setUseRebateCard(checked)}
                />
              </div>
              <div className="text-xs text-semi-color-text-2 mt-1.5 leading-relaxed">
                {useRebateCard
                  ? '已开启：系统将在退单前自动匹配锁定卡包中的 110% 返利券，毫秒重抢带上并享受超额返现。注意：小蚕规则此券一旦使用立即退单也绝不返还，请确保立即在美团/饿了么下单就餐！'
                  : '默认关闭：不使用 110% 返利券。避免因临时变动退单导致珍贵返利神券永久作废。'}
              </div>
            </div>

            <Banner
              type="info"
              closeIcon={null}
              description="退单重抢前系统将自动进行饭票充足性与商户活动状态安全双检。若饭票为 0 或活动已终结，将自动拦截避让以保全当前订单名额。"
            />
          </div>
        )}
      </Modal>
    </div>
  );
};
