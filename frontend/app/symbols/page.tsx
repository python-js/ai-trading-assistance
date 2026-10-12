'use client';

import { useEffect, useState } from 'react';
import { Alert, Card, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { SymbolPool, api, fmtTs } from '@/lib/api';

type Row = { symbol: string; timeframe: string; n_rules: number; last_ts: string };

export default function SymbolsPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
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
  }, []);

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
        message="自助加币（搜索标的、入池、绑定规则）在 P1-5 提供；当前展示已有规则的池。"
      />
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
