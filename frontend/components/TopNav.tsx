'use client';

import { Menu } from 'antd';
import { usePathname, useRouter } from 'next/navigation';

const ITEMS = [
  { key: '/', label: '台账' },
  { key: '/signals', label: '信号' },
  { key: '/chart', label: 'K线' },
  { key: '/symbols', label: '自选池' },
];

export default function TopNav() {
  const pathname = usePathname();
  const router = useRouter();
  const selected = ITEMS.some((i) => i.key === pathname) ? pathname : '/';
  return (
    <header style={{ borderBottom: '1px solid #f0f0f0' }}>
      <div style={{ maxWidth: 1100, margin: '0 auto', display: 'flex', alignItems: 'center' }}>
        <span style={{ fontWeight: 600, padding: '0 24px 0 16px', whiteSpace: 'nowrap' }}>
          AI 交易助手
        </span>
        <Menu
          mode="horizontal"
          selectedKeys={[selected]}
          items={ITEMS}
          onClick={({ key }) => router.push(key)}
          style={{ flex: 1, borderBottom: 'none' }}
        />
      </div>
    </header>
  );
}
