'use client';

import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Select, Space, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { SymbolPool, addSymbol, api, fmtTs, searchSymbols } from '@/lib/api';

type Row = { symbol: string; timeframe: string; n_rules: number; last_ts: string };

export default function SymbolsPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [options, setOptions] = useState<{ value: string; label: string; disabled?: boolean }[]>([]);
  const [picked, setPicked] = useState<string>();
  const [tfs, setTfs] = useState<string[]>(['1h', '4h']);
  const [adding, setAdding] = useState(false);
  const seq = useRef(0);

  const load = () => {
    setLoading(true);
    api<SymbolPool[]>('/api/symbols')
      .then((pool) =>
        setRows(
          pool.flatMap((p) =>
            p.periods.map((x) => ({
              symbol: p.symbol,
              timeframe: x.timeframe,
              n_rules: x.n_rules,
              last_ts: x.last_ts,
            }))
          )
        )
      )
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const onSearch = (q: string) => {
    if (!q.trim()) return;
    const id = ++seq.current;
    searchSymbols(q)
      .then((hits) => {
        if (id !== seq.current) return;
        setOptions(
          hits.map((h) => ({
            value: h.symbol,
            label: h.in_pool ? `${h.symbol}（已入池）` : h.symbol,
            disabled: h.in_pool,
          }))
        );
      })
      .catch(() => {});
  };

  const onAdd = async () => {
    if (!picked) {
      message.warning('请先搜索并选择标的');
      return;
    }
    setAdding(true);
    try {
      const res = await addSymbol(picked, tfs);
      message.success(
        `已加入 ${res.symbol}：回填 ${res.bars} 根K线、回放信号 ${res.signals} 条`
      );
      setPicked(undefined);
      setOptions([]);
      load();
    } catch (e) {
      message.error(`加币失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setAdding(false);
    }
  };

  const columns: ColumnsType<Row> = [
    { title: '标的', dataIndex: 'symbol' },
    { title: '周期', dataIndex: 'timeframe', width: 100, render: (t: string) => <Tag>{t}</Tag> },
    { title: '启用规则数', dataIndex: 'n_rules', width: 110 },
    { title: '最新数据', dataIndex: 'last_ts', render: fmtTs, width: 180 },
  ];

  return (
    <Card title="自选池">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="搜索 OKX 现货（USDT 计价）自助加币：自动绑定所选周期默认规则（1h=RSI过滤+吊灯出场 / 4h=双均线交叉）、回填历史并回放信号，扫描器 ≤1 个周期自动纳入，无需重启。"
      />
      <Space wrap style={{ marginBottom: 16 }}>
        <Select
          showSearch
          style={{ width: 280 }}
          placeholder="搜索标的（如 DOGE / SOL）"
          filterOption={false}
          onSearch={onSearch}
          onChange={(v) => setPicked(v)}
          value={picked}
          options={options}
          notFoundContent={null}
        />
        <Checkbox.Group
          options={['1m', '1h', '4h']}
          value={tfs}
          onChange={(v) => setTfs(v as string[])}
        />
        <Button type="primary" loading={adding} onClick={onAdd}>
          加入自选
        </Button>
      </Space>
      <Table
        rowKey={(r) => `${r.symbol}|${r.timeframe}`}
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: '池为空' }}
      />
    </Card>
  );
}
