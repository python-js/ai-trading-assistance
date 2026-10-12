/** 后端 FastAPI 地址（部署/局域网时可改 BACKEND_ORIGIN）。 */
const backend = process.env.BACKEND_ORIGIN || 'http://127.0.0.1:8000';

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: '/api/:path*', destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
