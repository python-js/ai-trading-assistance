'use client';

import { useEffect, useRef, useState } from 'react';
import { Card, Space, Spin } from 'antd';
import { UTCTimestamp, createChart, type ISeriesApi, type Time } from 'lightweight-charts';
import PoolSelect from '@/components/PoolSelect';
import SignalTable from '@/components/SignalTable';
import { Ohlcv, Signal, api, priceDecimals } from '@/lib/api';

/** 图表刻度/十字线时间 → 北京时间（UTC+8 固定偏移）。 */
const fmtBjTick = (t: Time, withTime = true): string => {
  if (typeof t !== 'number') return String(t);
  const iso = new Date((t + 8 * 3600) * 1000).toISOString();
  return withTime ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : iso.slice(5, 10);
};

const LIVE_POLL_MS = 5000;

type SeriesBundle = {
  candles: ISeriesApi<'Candlestick'>;
  sma5: ISeriesApi<'Line'>;
  sma10: ISeriesApi<'Line'>;
  sma20: ISeriesApi<'Line'>;
};
type LastTs = { bars: number; sma5: number; sma10: number; sma20: number };

/** 信号 → 图标记（买=下方绿箭头 / 卖=上方红箭头）。 */
const toMarkers = (signals: Ohlcv['signals']) =>
  [...signals]
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
    );

export default function ChartPage() {
  const [key, setKey] = useState<{ symbol: string; timeframe: string } | null>(null);
  const [data, setData] = useState<Ohlcv | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(false);
  const elRef = useRef<HTMLDivElement>(null);
  const seriesRef = useRef<SeriesBundle | null>(null);
  const lastTsRef = useRef<LastTs>({ bars: 0, sma5: 0, sma10: 0, sma20: 0 });
  const builtKeyRef = useRef('');
  const markerKeyRef = useRef('');

  const load = (k: { symbol: string; timeframe: string }) => {
    setLoading(true);
    Promise.all([
      api<Ohlcv>(
        `/api/ohlcv?symbol=${encodeURIComponent(k.symbol)}&timeframe=${k.timeframe}&limit=300&live=1`
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
    const markers = toMarkers(data.signals);
    markerKeyRef.current = `${markers.length}:${data.signals[data.signals.length - 1]?.signal_id ?? 0}`;
    candles.setMarkers(markers);
    seriesRef.current = { candles, sma5, sma10, sma20 };
    lastTsRef.current = {
      bars: data.bars[data.bars.length - 1]?.time ?? 0,
      sma5: data.sma5[data.sma5.length - 1]?.time ?? 0,
      sma10: data.sma10[data.sma10.length - 1]?.time ?? 0,
      sma20: data.sma20[data.sma20.length - 1]?.time ?? 0,
    };
    builtKeyRef.current = key ? `${key.symbol}|${key.timeframe}` : '';
    chart.timeScale().fitContent();
    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.remove();
      seriesRef.current = null;
    };
  }, [data]);

  // 盘中实时：5s 轮询（live=1），series.update 增量刷新当前蜡烛与三均线；不重建图表、不动视口。
  useEffect(() => {
    if (!key) return;
    const { symbol, timeframe } = key;
    const k = `${symbol}|${timeframe}`;
    const q = `symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=300&live=1`;
    const timer = setInterval(() => {
      if (document.hidden || !seriesRef.current || builtKeyRef.current !== k) return;
      api<Ohlcv>(`/api/ohlcv?${q}`)
        .then((res) => {
          const s = seriesRef.current;
          if (!s || builtKeyRef.current !== k) return;
          const last = lastTsRef.current;
          for (const b of res.bars) {
            if (b.time < last.bars) continue;
            s.candles.update({
              time: b.time as UTCTimestamp,
              open: b.open,
              high: b.high,
              low: b.low,
              close: b.close,
            });
            last.bars = b.time;
          }
          const lines: [ISeriesApi<'Line'>, { time: number; value: number }[],
            'sma5' | 'sma10' | 'sma20'][] = [
            [s.sma5, res.sma5, 'sma5'],
            [s.sma10, res.sma10, 'sma10'],
            [s.sma20, res.sma20, 'sma20'],
          ];
          for (const [series, arr, kk] of lines) {
            for (const p of arr) {
              if (p.time < last[kk]) continue;
              series.update({ time: p.time as UTCTimestamp, value: p.value });
              last[kk] = p.time;
            }
          }
          const ms = toMarkers(res.signals);
          const mk = `${ms.length}:${res.signals[res.signals.length - 1]?.signal_id ?? 0}`;
          if (mk !== markerKeyRef.current) {
            markerKeyRef.current = mk;
            s.candles.setMarkers(ms);
            api<Signal[]>(
              `/api/signals?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=50`
            )
              .then(setSignals)
              .catch(() => {});
          }
        })
        .catch(() => {}); // 单次失败静默，下轮自然重试
    }, LIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [key]);

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
