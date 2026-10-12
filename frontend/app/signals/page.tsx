'use client';

import { useEffect, useState } from 'react';
import { Button, Card, Space } from 'antd';
import PoolSelect from '@/components/PoolSelect';
import SignalTable from '@/components/SignalTable';
import { Signal, api } from '@/lib/api';

export default function SignalsPage() {
  const [key, setKey] = useState<{ symbol: string; timeframe: string } | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(false);

  const load = () => {
    if (!key) return;
    setLoading(true);
    api<Signal[]>(
      `/api/signals?symbol=${encodeURIComponent(key.symbol)}&timeframe=${key.timeframe}&limit=100`
    )
      .then(setSignals)
      .finally(() => setLoading(false));
  };

  useEffect(load, [key]);

  return (
    <Card
      title="信号"
      extra={<Button onClick={load}>刷新</Button>}
    >
      <Space wrap style={{ marginBottom: 12 }}>
        <PoolSelect onChange={(symbol, timeframe) => setKey({ symbol, timeframe })} />
      </Space>
      <SignalTable signals={signals} loading={loading} onChanged={load} />
    </Card>
  );
}
