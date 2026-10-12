'use client';

import { useState } from 'react';
import { Button, Space, Table, Tag, Tooltip, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Signal, fmtTs, postAnnotation } from '@/lib/api';

const ACTIONS = ['采纳', '忽略', '看错'] as const;

const fmtPrice = (x: number) => (x >= 1 ? x.toFixed(2) : Number(x).toPrecision(4));

export default function SignalTable({
  signals,
  loading,
  onChanged,
}: {
  signals: Signal[];
  loading?: boolean;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState<number | null>(null);

  const annotate = async (id: number, action: string) => {
    setBusy(id);
    try {
      await postAnnotation(id, action);
      message.success(`已标注：${action}`);
      onChanged?.();
    } catch (e) {
      message.error(`标注失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
    }
  };

  const columns: ColumnsType<Signal> = [
    { title: '时间', dataIndex: 'ts', render: fmtTs, width: 140 },
    { title: '标的', dataIndex: 'symbol', width: 110 },
    { title: '周期', dataIndex: 'timeframe', width: 70 },
    { title: '规则', dataIndex: 'rule', ellipsis: true },
    {
      title: '方向',
      dataIndex: 'direction',
      width: 80,
      render: (d: string) =>
        d === 'entry' ? <Tag color="green">入场</Tag> : <Tag color="red">出场</Tag>,
    },
    {
      title: '价格区间',
      width: 150,
      render: (_, s) => `${fmtPrice(s.price_zone_low)} ~ ${fmtPrice(s.price_zone_high)}`,
    },
    {
      title: '推送',
      dataIndex: 'pushed',
      width: 80,
      render: (n: number) => (n > 0 ? <Tag color="blue">已推送</Tag> : <Tag>未推送</Tag>),
    },
    {
      title: '标注',
      width: 180,
      render: (_, s) =>
        s.annotation ? (
          <Tag color="purple">{s.annotation}</Tag>
        ) : (
          <Space size={4}>
            {ACTIONS.map((a) => (
              <Button
                key={a}
                size="small"
                loading={busy === s.id}
                disabled={busy !== null && busy !== s.id}
                onClick={() => annotate(s.id, a)}
              >
                {a}
              </Button>
            ))}
          </Space>
        ),
    },
  ];

  return (
    <Table
      rowKey="id"
      size="small"
      loading={loading}
      columns={columns}
      dataSource={signals}
      pagination={{ pageSize: 20, showSizeChanger: false }}
      locale={{ emptyText: '暂无信号' }}
    />
  );
}
