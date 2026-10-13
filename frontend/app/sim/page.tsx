'use client';

import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  message,
} from 'antd';
import type { FormInstance } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { UTCTimestamp, createChart, type Time } from 'lightweight-charts';
import {
  RuleRow,
  SimAccountRow,
  SimDetail,
  SimResult,
  SimReview,
  SimTrade,
  SymbolPool,
  api,
  changeSimStrategy,
  createSim,
  deleteSim,
  fetchRules,
  fetchSim,
  fetchSims,
  finishSim,
  fmtPct,
  fmtPf,
  fmtPrice,
  fmtTs,
  pnlColor,
  reviewSim,
} from '@/lib/api';

const MARKET_CN: Record<string, string> = { crypto: '币圈', cn: 'A股' };
const END_CN: Record<string, string> = {
  stop_loss: '止损线强平',
  expired: '时间到期',
  manual: '手动结束',
};
const EXIT_CN: Record<string, string> = {
  signal: '信号',
  stop_loss: '止损线',
  expired: '到期',
  manual: '手动',
};

const simTradeCols: ColumnsType<SimTrade> = [
  { title: '标的', dataIndex: 'symbol', width: 110 },
  { title: '策略', dataIndex: 'strategy', ellipsis: true, width: 140 },
  { title: '入场时间', dataIndex: 'entry_ts', render: fmtTs, width: 140 },
  { title: '出场时间', dataIndex: 'exit_ts', render: fmtTs, width: 140 },
  {
    title: '入场价',
    dataIndex: 'entry_price',
    width: 110,
    render: (v: number, r) => fmtPrice(v, r.symbol),
  },
  {
    title: '出场价',
    dataIndex: 'exit_price',
    width: 110,
    render: (v: number, r) => fmtPrice(v, r.symbol),
  },
  {
    title: '盈亏',
    dataIndex: 'pnl',
    width: 90,
    render: (v: number) => <span style={{ color: pnlColor(v) }}>{v.toFixed(1)}</span>,
  },
  {
    title: '盈亏%',
    dataIndex: 'pnl_pct',
    width: 80,
    render: (v: number) => <span style={{ color: pnlColor(v) }}>{fmtPct(v)}</span>,
  },
  { title: '持有(h)', dataIndex: 'hold_hours', width: 80, render: (v: number) => v.toFixed(1) },
  {
    title: '出场原因',
    dataIndex: 'exit_reason',
    width: 90,
    render: (v: string) => EXIT_CN[v] ?? v,
  },
];

const openPosCols: ColumnsType<SimResult['open_pos'][number]> = [
  { title: '标的', dataIndex: 'symbol', width: 110 },
  { title: '周期', dataIndex: 'timeframe', width: 70 },
  { title: '策略', dataIndex: 'strategy', ellipsis: true },
  { title: '入场时间', dataIndex: 'entry_ts', render: fmtTs, width: 140 },
  {
    title: '入场价',
    dataIndex: 'entry_price',
    width: 110,
    render: (v: number, p) => fmtPrice(v, p.symbol),
  },
  {
    title: '浮动盈亏',
    dataIndex: 'unrealized',
    width: 110,
    render: (v: number) => <span style={{ color: pnlColor(v) }}>{v.toFixed(1)}</span>,
  },
];

const byUnitCols: ColumnsType<SimReview['by_unit'][number]> = [
  { title: '标的', dataIndex: 'symbol' },
  { title: '周期', dataIndex: 'timeframe', width: 70 },
  { title: '笔数', dataIndex: 'n', width: 70 },
  { title: '胜率', dataIndex: 'win_rate', width: 90, render: (v: number | null) => fmtPct(v) },
  { title: '盈亏比', dataIndex: 'pf', width: 90, render: (v: number | null) => fmtPf(v) },
  {
    title: '累计盈亏',
    dataIndex: 'total_pnl',
    width: 110,
    render: (v: number) => <span style={{ color: pnlColor(v) }}>{v.toFixed(1)}</span>,
  },
];

const bySegCols: ColumnsType<SimReview['by_segment'][number]> = [
  { title: '标的', dataIndex: 'symbol', width: 110 },
  { title: '周期', dataIndex: 'timeframe', width: 70 },
  { title: '策略', dataIndex: 'strategy', ellipsis: true },
  { title: '笔数', dataIndex: 'n', width: 70 },
  { title: '胜率', dataIndex: 'win_rate', width: 90, render: (v: number | null) => fmtPct(v) },
  { title: '盈亏比', dataIndex: 'pf', width: 90, render: (v: number | null) => fmtPf(v) },
  {
    title: '累计盈亏',
    dataIndex: 'total_pnl',
    width: 110,
    render: (v: number) => <span style={{ color: pnlColor(v) }}>{v.toFixed(1)}</span>,
  },
];

/** 图表刻度/十字线时间 → 北京时间（UTC+8 固定偏移）。 */
const fmtBj = (t: Time): string => {
  if (typeof t !== 'number') return String(t);
  const iso = new Date((t + 8 * 3600) * 1000).toISOString();
  return `${iso.slice(5, 10)} ${iso.slice(11, 16)}`;
};

/** 净值曲线（equity 已为 unix 秒）。 */
function EquityChart({ pts }: { pts: { time: number; value: number }[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || pts.length === 0) return;
    const chart = createChart(el, {
      height: 220,
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
      localization: { timeFormatter: fmtBj },
      timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: fmtBj },
    });
    const s = chart.addAreaSeries({
      lineColor: '#2f80ed',
      topColor: 'rgba(47,128,237,0.25)',
      bottomColor: 'rgba(47,128,237,0)',
      priceLineVisible: false,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    s.setData(pts.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    chart.timeScale().fitContent();
    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [pts]);
  return <div ref={ref} style={{ width: '100%' }} />;
}

type UnitForm = { symbol?: string; timeframe?: string; rule_id?: number };
type CreateForm = {
  name: string;
  init_cash: number;
  size_pct: number;
  stop_loss_pct: number;
  start_days_ago: number;
  duration_days?: number | null;
  units: UnitForm[];
};

/** 创建表单的单行：标的 + 周期 + 可选规则（联动过滤）。 */
function UnitRow({
  form,
  name,
  pool,
  rules,
  onRemove,
}: {
  form: FormInstance<CreateForm>;
  name: number;
  pool: SymbolPool[];
  rules: RuleRow[];
  onRemove: () => void;
}) {
  const symbol = Form.useWatch(['units', name, 'symbol'], form) as string | undefined;
  const timeframe = Form.useWatch(['units', name, 'timeframe'], form) as string | undefined;
  const periods = pool.find((p) => p.symbol === symbol)?.periods ?? [];
  const matched = rules.filter((r) => r.symbol === symbol && r.timeframe === timeframe);
  return (
    <Space align="baseline" wrap>
      <Form.Item name={[name, 'symbol']} rules={[{ required: true, message: '选择标的' }]}>
        <Select
          style={{ width: 180 }}
          placeholder="标的"
          showSearch
          options={pool.map((p) => ({ value: p.symbol, label: p.symbol }))}
          onChange={() => {
            form.setFieldValue(['units', name, 'timeframe'], undefined);
            form.setFieldValue(['units', name, 'rule_id'], undefined);
          }}
        />
      </Form.Item>
      <Form.Item name={[name, 'timeframe']} rules={[{ required: true, message: '周期' }]}>
        <Select
          style={{ width: 110 }}
          placeholder="周期"
          options={periods.map((x) => ({ value: x.timeframe, label: x.timeframe }))}
        />
      </Form.Item>
      <Form.Item
        name={[name, 'rule_id']}
        tooltip="选填：绑定自选池现有规则（含草稿）；留空用 sma_520 默认模板"
      >
        <Select
          style={{ width: 210 }}
          allowClear
          placeholder="策略（默认模板）"
          options={matched.map((r) => ({
            value: r.id,
            label: `${r.name}${r.enabled ? '' : '（草稿）'}`,
          }))}
        />
      </Form.Item>
      <Button size="small" danger onClick={onRemove}>
        删除
      </Button>
    </Space>
  );
}

export default function SimPage() {
  const [rows, setRows] = useState<SimAccountRow[]>([]);
  const [pool, setPool] = useState<SymbolPool[]>([]);
  const [rules, setRules] = useState<RuleRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [form] = Form.useForm<CreateForm>();
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [detail, setDetail] = useState<{ id: number; data: SimDetail | null } | null>(null);
  const [rev, setRev] = useState<{ data: SimReview; llm: boolean } | null>(null);
  const [revBusy, setRevBusy] = useState<'stats' | 'llm' | ''>('');
  const [chg, setChg] = useState<{ unit_idx: number; rule_id?: number; text?: string }>({
    unit_idx: 0,
  });
  const [chgBusy, setChgBusy] = useState(false);

  const load = () => {
    setLoading(true);
    fetchSims()
      .then(setRows)
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    load();
    api<SymbolPool[]>('/api/symbols').then(setPool);
    fetchRules().then(setRules);
  }, []);

  const submitCreate = async () => {
    let v: CreateForm;
    try {
      v = await form.validateFields();
    } catch {
      return;
    }
    setCreating(true);
    try {
      const r = await createSim({
        name: v.name.trim(),
        init_cash: v.init_cash,
        size_pct: v.size_pct,
        stop_loss_pct: v.stop_loss_pct,
        start_days_ago: v.start_days_ago ?? 0,
        duration_days: v.duration_days ?? null,
        units: v.units.map((u) => ({
          symbol: u.symbol ?? '',
          timeframe: u.timeframe ?? '',
          ...(u.rule_id ? { rule_id: u.rule_id } : {}),
        })),
      });
      message.success(`已创建「${r.name}」（${r.units.length} 个标的）`);
      setCreateOpen(false);
      form.resetFields();
      load();
    } catch (e) {
      message.error(`创建失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setCreating(false);
    }
  };

  const openDetail = (id: number) => {
    setDetail({ id, data: null });
    setRev(null);
    setChg({ unit_idx: 0 });
    fetchSim(id)
      .then((d) => setDetail((cur) => (cur && cur.id === id ? { ...cur, data: d } : cur)))
      .catch((e) => {
        message.error(`加载失败：${e instanceof Error ? e.message : e}`);
        setDetail(null);
      });
  };

  const reloadDetail = () => {
    if (!detail) return;
    fetchSim(detail.id)
      .then((d) => setDetail({ id: detail.id, data: d }))
      .catch((e) => message.error(`刷新失败：${e instanceof Error ? e.message : e}`));
  };

  const loadReview = async (llm: boolean) => {
    if (!detail) return;
    setRevBusy(llm ? 'llm' : 'stats');
    try {
      setRev({ data: await reviewSim(detail.id, llm), llm });
    } catch (e) {
      message.error(`复盘失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setRevBusy('');
    }
  };

  const submitChange = async () => {
    if (!detail?.data) return;
    const u = detail.data.units[chg.unit_idx];
    const text = (chg.text ?? '').trim();
    if (!chg.rule_id && !text) {
      message.warning('请选择规则或输入中文想法');
      return;
    }
    setChgBusy(true);
    try {
      const r = await changeSimStrategy(
        detail.id,
        chg.rule_id
          ? { unit_idx: chg.unit_idx, rule_id: chg.rule_id }
          : { unit_idx: chg.unit_idx, text }
      );
      message.success(`「${u.symbol} ${u.timeframe}」策略已调整为 ${r.name}（从下一根 bar 起生效）`);
      setChg({ unit_idx: chg.unit_idx });
      setRev(null);
      reloadDetail();
    } catch (e) {
      message.error(`调整失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setChgBusy(false);
    }
  };

  const onFinish = async (row: SimAccountRow) => {
    try {
      await finishSim(row.id);
      message.success(`已结束「${row.name}」：持仓按最后收盘价清算`);
      load();
    } catch (e) {
      message.error(`结束失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const onDelete = async (row: SimAccountRow) => {
    try {
      await deleteSim(row.id);
      message.success(`已删除「${row.name}」`);
      load();
    } catch (e) {
      message.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const columns: ColumnsType<SimAccountRow> = [
    { title: '名称', dataIndex: 'name', ellipsis: true, width: 130 },
    {
      title: '市场',
      dataIndex: 'market',
      width: 60,
      render: (v: string) => <Tag color={v === 'crypto' ? 'blue' : 'gold'}>{MARKET_CN[v] ?? v}</Tag>,
    },
    {
      title: '初始资金',
      dataIndex: 'init_cash',
      width: 100,
      render: (v: number, r) => `${v.toFixed(0)} ${r.market === 'crypto' ? 'USDT' : 'CNY'}`,
    },
    { title: '仓位%', dataIndex: 'size_pct', width: 70, render: (v: number) => `${v}%` },
    {
      title: '止损线',
      dataIndex: 'stop_loss_pct',
      width: 70,
      render: (v: number) => (v > 0 ? `-${v}%` : '关闭'),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: (v: string) =>
        v === 'active' ? <Tag color="processing">进行中</Tag> : <Tag>已结束</Tag>,
    },
    {
      title: '起止',
      width: 140,
      render: (_, r) => (
        <>
          <div>{fmtTs(r.start_ts)}</div>
          <div>→ {r.end_ts ? fmtTs(r.end_ts) : '不限'}</div>
        </>
      ),
    },
    {
      title: '标的',
      ellipsis: true,
      render: (_, r) => r.units.map((u) => `${u.symbol} ${u.timeframe}`).join('、'),
    },
    {
      title: '操作',
      width: 170,
      render: (_, r) => (
        <Space size={4}>
          <Button size="small" onClick={() => openDetail(r.id)}>
            详情
          </Button>
          {r.status === 'active' && (
            <Popconfirm
              title="结束该模拟盘？"
              description="持仓将按最后收盘价清算，之后不可再调整策略。"
              onConfirm={() => onFinish(r)}
            >
              <Button size="small">结束</Button>
            </Popconfirm>
          )}
          <Popconfirm title="删除该模拟盘？" onConfirm={() => onDelete(r)}>
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const d = detail?.data;
  const unitLabel = (symbol: string, timeframe: string) => `${symbol} ${timeframe}`;

  return (
    <Card
      title="模拟盘"
      extra={
        <Space>
          <Button onClick={load}>刷新</Button>
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            新建模拟盘
          </Button>
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="多标的共享一份资金池：每笔名义 = 当前权益 × 固定比例（复利）；成本模型与实盘台账同口径；起点在过去 → 立即回放出完整结果，起点在现在 → 从创建时刻起逐根跟随（同一套引擎）；止损线触发或到期即冻结清算。打开详情按需重放（确定性），无常驻任务。"
      />
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: '暂无模拟盘' }}
      />

      <Modal
        title="新建模拟盘"
        open={createOpen}
        onOk={submitCreate}
        confirmLoading={creating}
        onCancel={() => {
          setCreateOpen(false);
          form.resetFields();
        }}
        width={720}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            init_cash: 10000,
            size_pct: 30,
            stop_loss_pct: 50,
            start_days_ago: 0,
            units: [{}],
          }}
        >
          <div style={{ display: 'flex', gap: 12 }}>
            <Form.Item
              name="name"
              label="名称"
              style={{ flex: 2 }}
              rules={[{ required: true, message: '填写名称（≤50 字）' }]}
            >
              <Input maxLength={50} placeholder="如：BTC/ETH 双币试验" />
            </Form.Item>
            <Form.Item
              name="init_cash"
              label="初始资金"
              style={{ flex: 1 }}
              tooltip="币圈=USDT、A股=CNY，由标的自动判定"
              rules={[{ required: true, message: '填初始资金' }]}
            >
              <InputNumber min={1} style={{ width: '100%' }} />
            </Form.Item>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <Form.Item name="size_pct" label="每笔仓位 %（复利）" style={{ flex: 1 }}>
              <InputNumber min={0.1} max={100} step={5} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="stop_loss_pct"
              label="止损线 %"
              style={{ flex: 1 }}
              tooltip="权益跌破 初始资金 ×(1-x%) 即全部强平并结束；0=关闭"
            >
              <InputNumber min={0} max={99} style={{ width: '100%' }} />
            </Form.Item>
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <Form.Item
              name="start_days_ago"
              label="起点（天前）"
              style={{ flex: 1 }}
              tooltip="0 = 从创建时刻起逐根跟随；>0 = 从过去某天起立即回放出完整结果"
            >
              <InputNumber min={0} max={730} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item
              name="duration_days"
              label="期限（天）"
              style={{ flex: 1 }}
              tooltip="从创建时刻起 N 天后自动清算结束；留空=不限"
            >
              <InputNumber min={1} max={730} style={{ width: '100%' }} placeholder="留空=不限" />
            </Form.Item>
          </div>
          <Form.Item label="标的与策略（同一市场；共享资金池）" required style={{ marginBottom: 0 }}>
            <Form.List name="units">
              {(fields, { add, remove }) => (
                <>
                  {fields.map(({ key, name }) => (
                    <UnitRow
                      key={key}
                      form={form}
                      name={name}
                      pool={pool}
                      rules={rules}
                      onRemove={() => remove(name)}
                    />
                  ))}
                  <Button type="dashed" onClick={() => add()} block>
                    + 添加标的
                  </Button>
                </>
              )}
            </Form.List>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`模拟盘 · ${d?.name ?? ''}`}
        open={detail !== null}
        footer={null}
        onCancel={() => setDetail(null)}
        width={980}
      >
        {detail && (
          <div style={{ maxHeight: '72vh', overflowY: 'auto', paddingRight: 8 }}>
            {!d ? (
              <div style={{ textAlign: 'center', padding: 48 }}>
                <Spin />
              </div>
            ) : (
              <>
                <Space style={{ marginBottom: 12 }}>
                  <Button size="small" onClick={reloadDetail}>
                    刷新重放
                  </Button>
                </Space>
                <Descriptions size="small" column={3} style={{ marginBottom: 8 }}>
                  <Descriptions.Item label="市场">
                    {MARKET_CN[d.market] ?? d.market}（{d.market === 'crypto' ? 'USDT' : 'CNY'}）
                  </Descriptions.Item>
                  <Descriptions.Item label="初始资金">{d.init_cash.toFixed(0)}</Descriptions.Item>
                  <Descriptions.Item label="每笔仓位">{d.size_pct}%</Descriptions.Item>
                  <Descriptions.Item label="止损线">
                    {d.stop_loss_pct > 0 ? `-${d.stop_loss_pct}%` : '关闭'}
                  </Descriptions.Item>
                  <Descriptions.Item label="起点">{fmtTs(d.start_ts)}</Descriptions.Item>
                  <Descriptions.Item label="终点">
                    {d.end_ts ? fmtTs(d.end_ts) : '不限'}
                  </Descriptions.Item>
                  <Descriptions.Item label="状态">
                    {d.result.status === 'active' ? (
                      <Tag color="processing">进行中</Tag>
                    ) : (
                      <Tag>已结束</Tag>
                    )}
                    {d.result.end_reason && `（${END_CN[d.result.end_reason] ?? d.result.end_reason}）`}
                  </Descriptions.Item>
                  <Descriptions.Item label="当前现金">
                    {d.result.cash.toFixed(1)}
                  </Descriptions.Item>
                  <Descriptions.Item label="已实现盈亏">
                    <span style={{ color: pnlColor(d.result.realized) }}>
                      {d.result.realized.toFixed(1)}
                    </span>
                  </Descriptions.Item>
                  <Descriptions.Item label="交易笔数">
                    {d.result.metrics.n_trades}
                  </Descriptions.Item>
                  <Descriptions.Item label="胜率">
                    {fmtPct(d.result.metrics.win_rate)}
                  </Descriptions.Item>
                  <Descriptions.Item label="盈亏比">
                    {fmtPf(d.result.metrics.pf)}
                  </Descriptions.Item>
                  <Descriptions.Item label="累计盈亏">
                    <span style={{ color: pnlColor(d.result.metrics.total_pnl) }}>
                      {d.result.metrics.total_pnl.toFixed(1)}
                    </span>
                  </Descriptions.Item>
                  <Descriptions.Item label="收益率">
                    {d.result.metrics['return_on_init[%]'].toFixed(2)}%
                  </Descriptions.Item>
                  <Descriptions.Item label="最大回撤">
                    {d.result.metrics['max_drawdown[%]'].toFixed(2)}%
                  </Descriptions.Item>
                  <Descriptions.Item label="费用合计">
                    {d.result.metrics.fees_total.toFixed(1)}
                  </Descriptions.Item>
                  <Descriptions.Item label="平均持仓">
                    {d.result.metrics.avg_hold_hours == null
                      ? '-'
                      : `${d.result.metrics.avg_hold_hours.toFixed(1)} 小时`}
                  </Descriptions.Item>
                  <Descriptions.Item label="暴露">
                    {d.result.metrics['exposure[%]'].toFixed(0)}%
                  </Descriptions.Item>
                </Descriptions>

                <Divider style={{ margin: '12px 0' }}>净值曲线</Divider>
                {d.result.equity.length > 0 ? (
                  <EquityChart pts={d.result.equity} />
                ) : (
                  <div style={{ color: '#999' }}>暂无净值数据</div>
                )}

                {d.result.open_pos.length > 0 && (
                  <>
                    <Divider style={{ margin: '12px 0' }}>持仓中</Divider>
                    <Table
                      rowKey={(p) => `${p.unit_idx}`}
                      size="small"
                      pagination={false}
                      dataSource={d.result.open_pos}
                      columns={openPosCols}
                    />
                  </>
                )}

                <Divider style={{ margin: '12px 0' }}>交易明细</Divider>
                <Table
                  rowKey={(t) => `${t.unit_idx}|${t.entry_ts}|${t.exit_ts}`}
                  size="small"
                  columns={simTradeCols}
                  dataSource={d.result.trades}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  locale={{ emptyText: '区间内无成交' }}
                />

                <Divider style={{ margin: '12px 0' }}>策略与调整</Divider>
                <div style={{ marginBottom: 8, fontSize: 13, color: '#555' }}>
                  {d.units.map((u) => (
                    <div key={`${u.symbol}|${u.timeframe}`}>
                      {unitLabel(u.symbol, u.timeframe)}：
                      {u.changes
                        .map((c) => `${c.name}（${fmtTs(c.effective_from)} 起）`)
                        .join(' → ')}
                    </div>
                  ))}
                </div>
                {d.db_status === 'finished' ? (
                  <div style={{ color: '#999' }}>已结束：不可再调整策略</div>
                ) : (
                  <>
                    <Space wrap>
                      <Select
                        size="small"
                        style={{ width: 200 }}
                        value={chg.unit_idx}
                        onChange={(v) => setChg({ unit_idx: v })}
                        options={d.units.map((u, i) => ({
                          value: i,
                          label: unitLabel(u.symbol, u.timeframe),
                        }))}
                      />
                      <Select
                        size="small"
                        style={{ width: 210 }}
                        allowClear
                        placeholder="选择规则（或填右侧想法）"
                        value={chg.rule_id}
                        onChange={(v) => setChg((cur) => ({ ...cur, rule_id: v }))}
                        options={rules
                          .filter(
                            (r) =>
                              r.symbol === d.units[chg.unit_idx]?.symbol &&
                              r.timeframe === d.units[chg.unit_idx]?.timeframe
                          )
                          .map((r) => ({
                            value: r.id,
                            label: `${r.name}${r.enabled ? '' : '（草稿）'}`,
                          }))}
                      />
                      <Input
                        size="small"
                        style={{ width: 240 }}
                        placeholder="未选规则时用此中文想法生成"
                        maxLength={500}
                        value={chg.text}
                        onChange={(e) => setChg((cur) => ({ ...cur, text: e.target.value }))}
                      />
                      <Button size="small" type="primary" loading={chgBusy} onClick={submitChange}>
                        保存调整
                      </Button>
                    </Space>
                    <div style={{ marginTop: 6, fontSize: 12, color: '#999' }}>
                      从下一根 bar 起生效；换策略后旧持仓由新策略的出场条件管理。
                    </div>
                  </>
                )}

                <Divider style={{ margin: '12px 0' }}>复盘</Divider>
                <Space style={{ marginBottom: 8 }}>
                  <Button
                    size="small"
                    loading={revBusy === 'stats'}
                    onClick={() => loadReview(false)}
                  >
                    确定性复盘
                  </Button>
                  <Button
                    size="small"
                    loading={revBusy === 'llm'}
                    onClick={() => loadReview(true)}
                  >
                    AI 改进建议
                  </Button>
                </Space>
                {rev && (
                  <Space direction="vertical" style={{ width: '100%' }} size={8}>
                    {rev.data.llm.ok ? (
                      <Alert
                        type="success"
                        showIcon
                        message="AI 改进建议"
                        description={rev.data.llm.text}
                      />
                    ) : (
                      rev.llm && (
                        <Alert
                          type="warning"
                          showIcon
                          message="AI 建议不可用（已降级为确定性统计）"
                          description={rev.data.llm.error}
                        />
                      )
                    )}
                    {rev.data.notes.length > 0 && (
                      <Alert
                        type="info"
                        showIcon
                        message="归因要点"
                        description={
                          <ul style={{ margin: 0, paddingLeft: 18 }}>
                            {rev.data.notes.map((n, i) => (
                              <li key={i}>{n}</li>
                            ))}
                          </ul>
                        }
                      />
                    )}
                    <Table
                      rowKey={(r) => `${r.symbol}|${r.timeframe}`}
                      size="small"
                      title={() => '按标的'}
                      columns={byUnitCols}
                      dataSource={rev.data.by_unit}
                      pagination={false}
                      locale={{ emptyText: '无成交' }}
                    />
                    {rev.data.by_segment.length > rev.data.by_unit.length && (
                      <Table
                        rowKey={(r) => `${r.unit_idx}|${r.strategy}`}
                        size="small"
                        title={() => '按策略段（切换前后对照）'}
                        columns={bySegCols}
                        dataSource={rev.data.by_segment}
                        pagination={false}
                      />
                    )}
                  </Space>
                )}
              </>
            )}
          </div>
        )}
      </Modal>
    </Card>
  );
}
