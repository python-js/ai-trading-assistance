'use client';

import { useEffect, useRef, useState } from 'react';
import { Card, Space, Spin } from 'antd';
import { UTCTimestamp, createChart, type Time } from 'lightweight-charts';
import PoolSelect from '@/components/PoolSelect';
import SignalTable from '@/components/SignalTable';
import { Ohlcv, Signal, api, priceDecimals } from '@/lib/api';

/** 图表刻度/十字线时间 → 北京时间（UTC+8 固定偏移）。 */
const fmtBjTick = (t: Time, withTime = true): string => {
  if (typeof t !== 'number') return String(t);
  const iso = new Date((t + 8 * 3600) * 1000).toISOString();
  return withTime ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : iso.slice(5, 10);
};

export default function ChartPage() {
  const [key, setKey] = useState<{ symbol: string; timeframe: string } | null>(null);
  const [data, setData] = useState<Ohlcv | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(false);
  const elRef = useRef<HTMLDivElement>(null);

  const load = (k: { symbol: string; timeframe: string }) => {
    setLoading(true);
    Promise.all([
      api<Ohlcv>(
        `/api/ohlcv?symbol=${encodeURIComponent(k.symbol)}&timeframe=${k.timeframe}&limit=300`
      ),
      api<Signal[]>(
        `/api/signals?symbol=${encodeURIComponent(k.symbol)}&timeframe=${k.timeframe}&limit=50`
      ),
    ])
      .then(([d, s]) => {
        setData(d);
        setSignals(s);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (key) load(key);
  }, [key]);

  useEffect(() => {
    const el = elRef.current;
    if (!data || !el) return;
    const dec = priceDecimals(data.bars[data.bars.length - 1]?.close ?? 0, key?.symbol);
    const priceFormat = { type: 'price' as const, precision: dec, minMove: 10 ** -dec };
    const chart = createChart(el, {
      height: 460,
      layout: { background: { color: '#ffffff' }, textColor: '#333' },
      grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
      localization: { timeFormatter: (t: Time) => fmtBjTick(t) },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (t: Time) => fmtBjTick(t, key?.timeframe !== '1d'),
      },
    });
    const candles = chart.addCandlestickSeries({
      upColor: '#3f8600',
      downColor: '#cf1322',
      wickUpColor: '#3f8600',
      wickDownColor: '#cf1322',
      borderVisible: false,
      priceFormat,
    });
    candles.setData(
      data.bars.map((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      }))
    );
    const sma20 = chart.addLineSeries({
      color: '#2f80ed',
      lineWidth: 1,
      priceLineVisible: false,
      priceFormat,
    });
    sma20.setData(data.sma20.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    const sma10 = chart.addLineSeries({
      color: '#eb2f96',
      lineWidth: 1,
      priceLineVisible: false,
      priceFormat,
    });
    sma10.setData(data.sma10.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    const sma5 = chart.addLineSeries({
      color: '#f5a623',
      lineWidth: 1,
      priceLineVisible: false,
      priceFormat,
    });
    sma5.setData(data.sma5.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    candles.setMarkers(
      [...data.signals]
        .sort((a, b) => a.time - b.time)
        .map((s) =>
          s.direction === 'entry'
            ? {
                time: s.time as UTCTimestamp,
                position: 'belowBar' as const,
                color: '#3f8600',
                shape: 'arrowUp' as const,
                text: '买',
              }
            : {
                time: s.time as UTCTimestamp,
                position: 'aboveBar' as const,
                color: '#cf1322',
                shape: 'arrowDown' as const,
                text: '卖',
              }
        )
    );
    chart.timeScale().fitContent();
    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [data]);

  return (
    <Spin spinning={loading}>
      <Card title="K线" extra={<PoolSelect onChange={(symbol, timeframe) => setKey({ symbol, timeframe })} />}>
        <div style={{ marginBottom: 6, fontSize: 12, color: '#666' }}>
          {[
            { label: 'MA5', color: '#f5a623' },
            { label: 'MA10', color: '#eb2f96' },
            { label: 'MA20', color: '#2f80ed' },
          ].map((m) => (
            <span key={m.label} style={{ marginRight: 20 }}>
              <span
                style={{
                  display: 'inline-block',
                  width: 20,
                  height: 3,
                  background: m.color,
                  verticalAlign: 'middle',
                  marginRight: 4,
                }}
              />
              {m.label}
            </span>
          ))}
        </div>
        <div ref={elRef} style={{ width: '100%' }} />
      </Card>
      <Card title="本标的最近信号（可标注）" size="small" style={{ marginTop: 16 }}>
        <SignalTable signals={signals} onChanged={() => key && load(key)} />
      </Card>
    </Spin>
  );
}
