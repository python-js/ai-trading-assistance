'use client';

import { useEffect, useState } from 'react';
import { Select, Space } from 'antd';
import { SymbolPool, api } from '@/lib/api';

/** 标的 + 周期选择器：数据来自 /api/symbols（当前启用规则的池）。 */
export default function PoolSelect({
  onChange,
}: {
  onChange: (symbol: string, timeframe: string) => void;
}) {
  const [pool, setPool] = useState<SymbolPool[]>([]);
  const [symbol, setSymbol] = useState<string>();
  const [timeframe, setTimeframe] = useState<string>();

  useEffect(() => {
    api<SymbolPool[]>('/api/symbols').then((p) => {
      setPool(p);
      const s = p[0];
      const tf = s?.periods[0]?.timeframe;
      if (s && tf) {
        setSymbol(s.symbol);
        setTimeframe(tf);
        onChange(s.symbol, tf);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pick = (s: string, tf?: string) => {
    const p = pool.find((x) => x.symbol === s);
    const t = tf && p?.periods.some((y) => y.timeframe === tf) ? tf : p?.periods[0]?.timeframe;
    setSymbol(s);
    setTimeframe(t);
    if (t) onChange(s, t);
  };

  const periods = pool.find((x) => x.symbol === symbol)?.periods ?? [];

  return (
    <Space wrap>
      <Select
        style={{ width: 200 }}
        value={symbol}
        onChange={(v) => pick(v)}
        options={pool.map((p) => ({ value: p.symbol, label: p.symbol }))}
        placeholder="标的"
        showSearch
      />
      <Select
        style={{ width: 100 }}
        value={timeframe}
        onChange={(v) => symbol && pick(symbol, v)}
        options={periods.map((x) => ({
          value: x.timeframe,
          label: `${x.timeframe}（${x.n_rules}规则）`,
        }))}
        placeholder="周期"
      />
    </Space>
  );
}
