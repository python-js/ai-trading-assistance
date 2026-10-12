'use client';

import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import PoolSelect from '@/components/PoolSelect';
import {
  BacktestResult,
  BacktestTrade,
  RuleRow,
  createRuleDraft,
  fetchRules,
  fmtPct,
  fmtPf,
  fmtPrice,
  fmtTs,
  pnlColor,
  runBacktest,
  toggleRule,
} from '@/lib/api';

const DSL_STYLE: React.CSSProperties = {
  fontFamily: 'monospace',
  background: '#f6f6f6',
  padding: 12,
  borderRadius: 6,
  maxHeight: 420,
  overflow: 'auto',
};

const tradeCols = (symbol?: string): ColumnsType<BacktestTrade> => [
  { title: '入场时间', dataIndex: 'entry_ts', render: fmtTs, width: 140 },
  { title: '出场时间', dataIndex: 'exit_ts', render: fmtTs, width: 140 },
  { title: '入场价', dataIndex: 'entry_price', render: (v: number) => fmtPrice(v, symbol), width: 100 },
  { title: '出场价', dataIndex: 'exit_price', render: (v: number) => fmtPrice(v, symbol), width: 100 },
  {
    title: '盈亏',
    dataIndex: 'pnl',
    width: 90,
    render: (v: number) => <span style={{ color: pnlColor(v) }}>{v.toFixed(1)}</span>,
  },
  {
    title: '盈亏%',
    dataIndex: 'pnl_pct',
    width: 90,
    render: (v: number) => <span style={{ color: pnlColor(v) }}>{fmtPct(v)}</span>,
  },
  { title: '持仓(根)', dataIndex: 'hold_bars', width: 80 },
  { title: 'MAE%', dataIndex: 'mae', width: 80, render: (v: number) => fmtPct(v) },
];

export default function StrategyPage() {
  const [rules, setRules] = useState<RuleRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [key, setKey] = useState<{ symbol: string; timeframe: string } | null>(null);
  const [text, setText] = useState('');
  const [genBusy, setGenBusy] = useState(false);
  const [dslView, setDslView] = useState<{ name: string; dsl: Record<string, unknown> } | null>(
    null
  );
  const [bt, setBt] = useState<{
    row: RuleRow;
    days: number;
    data: BacktestResult | null;
    loading: boolean;
  } | null>(null);
  const [toggling, setToggling] = useState<number | null>(null);

  const load = () => {
    setLoading(true);
    fetchRules()
      .then(setRules)
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const generate = async () => {
    if (!key) {
      message.warning('请先选择标的与周期');
      return;
    }
    const t = text.trim();
    if (!t) {
      message.warning('请输入想法描述');
      return;
    }
    setGenBusy(true);
    try {
      const d = await createRuleDraft(t, key.symbol, key.timeframe);
      message.success('草稿已生成（默认停用）：先回测验证，再决定是否启用');
      setText('');
      load();
      setDslView({ name: d.name, dsl: d.dsl });
    } catch (e) {
      message.error(`生成失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setGenBusy(false);
    }
  };

  const onToggle = async (row: RuleRow, enabled: boolean) => {
    setToggling(row.id);
    try {
      await toggleRule(row.id, enabled);
      message.success(enabled ? `已启用 ${row.name}（扫描器下个周期纳入）` : `已停用 ${row.name}`);
      load();
    } catch (e) {
      message.error(`操作失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setToggling(null);
    }
  };

  const openBt = (row: RuleRow, days = 120) => {
    setBt({ row, days, data: null, loading: true });
    runBacktest(row.id, days)
      .then((data) => setBt((cur) => (cur && cur.row.id === row.id ? { ...cur, data, loading: false } : cur)))
      .catch((e) => {
        message.error(`回测失败：${e instanceof Error ? e.message : e}`);
        setBt(null);
      });
  };

  const rerun = (days: number) => {
    if (!bt) return;
    setBt({ ...bt, days, loading: true, data: null });
    runBacktest(bt.row.id, days)
      .then((data) => setBt((cur) => (cur && cur.row.id === bt.row.id ? { ...cur, data, loading: false } : cur)))
      .catch((e) => {
        message.error(`回测失败：${e instanceof Error ? e.message : e}`);
        setBt(null);
      });
  };

  const columns: ColumnsType<RuleRow> = [
    {
      title: '名称',
      dataIndex: 'name',
      ellipsis: true,
      render: (n: string, r) => (
        <>
          {n}
          {!r.enabled && <Tag color="gold" style={{ marginLeft: 8 }}>草稿</Tag>}
        </>
      ),
    },
    { title: '标的', dataIndex: 'symbol', width: 110 },
    { title: '周期', dataIndex: 'timeframe', width: 70, render: (t: string) => <Tag>{t}</Tag> },
    { title: '创建时间', dataIndex: 'created_ts', render: fmtTs, width: 140 },
    {
      title: '状态',
      width: 100,
      render: (_, r) => (
        <Switch
          size="small"
          checked={r.enabled}
          checkedChildren="启用"
          unCheckedChildren="停用"
          loading={toggling === r.id}
          onChange={(v) => onToggle(r, v)}
        />
      ),
    },
    {
      title: '操作',
      width: 150,
      render: (_, r) => (
        <Space size={4}>
          <Button size="small" onClick={() => setDslView({ name: r.name, dsl: r.dsl_json })}>
            DSL
          </Button>
          <Button size="small" onClick={() => openBt(r)}>
            回测
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card title="策略" extra={<Button onClick={load}>刷新</Button>}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="把中文交易想法交给 AI 生成规则草稿（入场/出场/冷却）。草稿默认停用：先沙箱回测（与实盘台账同口径：信号 bar 收盘成交、taker 0.05% + 滑点 5bps、名义 1000 USDT/笔）验证表现，满意再启用；启用后扫描器下个周期自动纳入。"
      />
      <Space wrap style={{ marginBottom: 8 }}>
        <PoolSelect onChange={(symbol, timeframe) => setKey({ symbol, timeframe })} />
      </Space>
      <Input.TextArea
        rows={2}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="用中文描述交易想法，如：RSI 低于 30 买入，高于 70 卖出，冷却 5 根"
        maxLength={500}
        showCount
        style={{ marginBottom: 8 }}
      />
      <div style={{ marginBottom: 16 }}>
        <Button type="primary" loading={genBusy} onClick={generate}>
          生成草稿
        </Button>
      </div>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rules}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: '暂无规则' }}
      />

      <Modal
        title={`规则 DSL · ${dslView?.name ?? ''}`}
        open={dslView !== null}
        footer={null}
        onCancel={() => setDslView(null)}
        width={640}
      >
        <pre style={DSL_STYLE}>{JSON.stringify(dslView?.dsl, null, 2)}</pre>
      </Modal>

      <Modal
        title={`沙箱回测 · ${bt?.row.name ?? ''}`}
        open={bt !== null}
        footer={null}
        onCancel={() => setBt(null)}
        width={880}
      >
        {bt && (
          <>
            <Space style={{ marginBottom: 12 }}>
              <span>区间：最近</span>
              <Select
                size="small"
                style={{ width: 90 }}
                value={bt.days}
                onChange={rerun}
                options={[30, 60, 120, 365].map((d) => ({ value: d, label: `${d} 天` }))}
              />
            </Space>
            {bt.loading ? (
              <div style={{ textAlign: 'center', padding: 32 }}>
                <Spin />
              </div>
            ) : (
              bt.data && (
                <>
                  <Descriptions size="small" column={3} style={{ marginBottom: 12 }}>
                    <Descriptions.Item label="交易笔数">{bt.data.metrics.n_trades}</Descriptions.Item>
                    <Descriptions.Item label="胜率">{fmtPct(bt.data.metrics.win_rate)}</Descriptions.Item>
                    <Descriptions.Item label="盈亏比">{fmtPf(bt.data.metrics.pf)}</Descriptions.Item>
                    <Descriptions.Item label="累计盈亏">
                      <span style={{ color: pnlColor(bt.data.metrics.total_pnl) }}>
                        {bt.data.metrics.total_pnl.toFixed(1)} USDT
                      </span>
                    </Descriptions.Item>
                    <Descriptions.Item label="收益率">
                      {bt.data.metrics['return_on_init[%]'].toFixed(2)}%
                    </Descriptions.Item>
                    <Descriptions.Item label="最大回撤">
                      {bt.data.metrics['max_drawdown[%]'].toFixed(2)}%
                    </Descriptions.Item>
                    <Descriptions.Item label="平均持仓">
                      {bt.data.metrics.avg_hold_bars == null
                        ? '-'
                        : `${bt.data.metrics.avg_hold_bars.toFixed(1)} 根`}
                    </Descriptions.Item>
                    <Descriptions.Item label="事件数">{bt.data.n_events}</Descriptions.Item>
                    <Descriptions.Item label="持仓中">
                      {bt.data.open_pos ? '是（未平仓）' : '否'}
                    </Descriptions.Item>
                  </Descriptions>
                  <Table
                    rowKey={(t) => `${t.entry_ts}|${t.exit_ts}`}
                    size="small"
                    columns={tradeCols(bt.row.symbol)}
                    dataSource={bt.data.trades}
                    pagination={{ pageSize: 8, showSizeChanger: false }}
                    locale={{ emptyText: '区间内无交易' }}
                  />
                </>
              )
            )}
          </>
        )}
      </Modal>
    </Card>
  );
}
