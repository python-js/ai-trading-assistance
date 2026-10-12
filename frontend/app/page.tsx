'use client';

import { useEffect, useState } from 'react';
import { Card, Col, Row, Segmented, Spin, Statistic, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Position, Report, ReportStats, api, fmtPct, fmtPf, fmtPrice, fmtTs, pnlColor } from '@/lib/api';

const statCols: ColumnsType<[string, ReportStats]> = [
  { title: '分组', render: (r) => r[0] },
  { title: '笔数', render: (r) => r[1].n, width: 80 },
  { title: '胜率', render: (r) => fmtPct(r[1].win_rate), width: 90 },
  { title: '盈亏比', render: (r) => fmtPf(r[1].pf), width: 90 },
  { title: '均收益', render: (r) => fmtPct(r[1].avg_pnl_pct), width: 100 },
  {
    title: '累计盈亏',
    render: (r) => (
      <span style={{ color: pnlColor(r[1].total_pnl) }}>{r[1].total_pnl.toFixed(2)}</span>
    ),
    width: 120,
  },
];

export default function LedgerPage() {
  const [days, setDays] = useState(14);
  const [rep, setRep] = useState<Report | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api<Report>(`/api/report?days=${days}`),
      api<Position[]>('/api/positions?status=all&limit=20'),
    ])
      .then(([r, p]) => {
        setRep(r);
        setPositions(p);
      })
      .finally(() => setLoading(false));
  }, [days]);

  const o = rep?.overall;

  return (
    <Spin spinning={loading}>
      <Card
        title="台账概览"
        extra={
          <Segmented
            value={days}
            onChange={(v) => setDays(v as number)}
            options={[
              { label: '近 7 天', value: 7 },
              { label: '近 14 天', value: 14 },
              { label: '近 30 天', value: 30 },
            ]}
          />
        }
      >
        <Row gutter={[16, 16]}>
          <Col xs={12} md={8} lg={4}>
            <Statistic title="平仓笔数" value={o?.n ?? '-'} />
          </Col>
          <Col xs={12} md={8} lg={5}>
            <Statistic title="胜率" value={o?.win_rate != null ? (o.win_rate * 100).toFixed(1) : '-'} suffix="%" />
          </Col>
          <Col xs={12} md={8} lg={5}>
            <Statistic title="盈亏比" value={fmtPf(o?.pf ?? null)} />
          </Col>
          <Col xs={12} md={12} lg={5}>
            <Statistic
              title="累计盈亏 (USDT)"
              value={o ? o.total_pnl.toFixed(2) : '-'}
              valueStyle={{ color: pnlColor(o?.total_pnl ?? null) }}
            />
          </Col>
          <Col xs={12} md={12} lg={5}>
            <Statistic title="单笔均收益" value={o?.avg_pnl_pct != null ? (o.avg_pnl_pct * 100).toFixed(2) : '-'} suffix="%" />
          </Col>
        </Row>
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
          口径：成交价=信号 bar 收盘价；成本 taker 0.05% + 滑点 5bps 单边；每笔名义 1000 USDT。
        </Typography.Text>
      </Card>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card title="按周期" size="small">
            <Table
              rowKey={(r) => r[0]}
              size="small"
              pagination={false}
              columns={statCols}
              dataSource={Object.entries(rep?.by_timeframe ?? {})}
            />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="按标的" size="small">
            <Table
              rowKey={(r) => r[0]}
              size="small"
              pagination={false}
              columns={statCols}
              dataSource={Object.entries(rep?.by_symbol ?? {})}
            />
          </Card>
        </Col>
      </Row>

      <Card title="最近持仓" size="small" style={{ marginTop: 16 }}>
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={positions}
          columns={[
            {
              title: '状态',
              dataIndex: 'status',
              width: 80,
              render: (s: string) =>
                s === 'open' ? <Tag color="processing">持仓中</Tag> : <Tag>已平</Tag>,
            },
            { title: '标的', dataIndex: 'symbol', width: 110 },
            { title: '周期', dataIndex: 'timeframe', width: 70 },
            { title: '入场', dataIndex: 'entry_ts', render: fmtTs, width: 140 },
            { title: '入场价', dataIndex: 'entry_price', render: (x: number, r: Position) => fmtPrice(x, r.symbol) },
            { title: '出场', dataIndex: 'exit_ts', render: fmtTs, width: 140 },
            {
              title: '盈亏',
              dataIndex: 'pnl',
              render: (x: number | null) => (
                <span style={{ color: pnlColor(x) }}>{x == null ? '-' : x.toFixed(2)}</span>
              ),
            },
            { title: '收益率', dataIndex: 'pnl_pct', render: (x: number | null) => fmtPct(x) },
          ]}
        />
      </Card>

      <Card title="企微报表预览（可复制）" size="small" style={{ marginTop: 16 }}>
        <Typography.Paragraph
          copyable={{ text: rep?.markdown ?? '' }}
          style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: 12, margin: 0 }}
        >
          {rep?.markdown ?? ''}
        </Typography.Paragraph>
      </Card>
    </Spin>
  );
}
