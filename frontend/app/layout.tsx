import type { Metadata } from 'next';
import { AntdRegistry } from '@ant-design/nextjs-registry';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import TopNav from '@/components/TopNav';
import 'antd/dist/reset.css';

export const metadata: Metadata = {
  title: 'AI 交易助手',
  description: '个人交易辅助：信号 / 台账 / 看板',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <AntdRegistry>
          <ConfigProvider locale={zhCN}>
            <TopNav />
            <main style={{ maxWidth: 1100, margin: '0 auto', padding: '16px 16px 48px' }}>
              {children}
            </main>
          </ConfigProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
