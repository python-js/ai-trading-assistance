'use client';

import { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, Popconfirm, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Knowledge, addKnowledge, deleteKnowledge, fetchKnowledge, fmtTs } from '@/lib/api';

export default function KnowledgePage() {
  const [items, setItems] = useState<Knowledge[]>([]);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState('');
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoading(true);
    fetchKnowledge()
      .then(setItems)
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const submit = async () => {
    const t = text.trim();
    if (!t) {
      message.warning('请先输入内容');
      return;
    }
    setSaving(true);
    try {
      await addKnowledge(t);
      message.success('已入库（向量化完成）');
      setText('');
      load();
    } catch (e) {
      message.error(`入库失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: number) => {
    try {
      await deleteKnowledge(id);
      load();
    } catch (e) {
      message.error(`删除失败：${e instanceof Error ? e.message : e}`);
    }
  };

  const columns: ColumnsType<Knowledge> = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    {
      title: '来源',
      dataIndex: 'source_type',
      width: 110,
      render: (t: string) =>
        t === 'manual' ? <Tag color="blue">手动</Tag> : <Tag color="purple">标注笔记</Tag>,
    },
    { title: '内容', dataIndex: 'text', ellipsis: true },
    { title: '时间', dataIndex: 'created_ts', render: fmtTs, width: 140 },
    {
      title: '操作',
      width: 80,
      render: (_, it) => (
        <Popconfirm title="删除该条目？" onConfirm={() => remove(it.id)}>
          <Button size="small" danger>
            删除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <Card title="个人研判知识库" extra={<Button onClick={load}>刷新</Button>}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="这里写入你的研判经验；推送生成理由前会按信号语义召回最相关的 3 条作为参考。信号页标注时填写的笔记会自动入此处，无需重复录入。"
      />
      <Input.TextArea
        rows={2}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="例如：BTC 4h 突破前高后回踩 EMA20 不破，多数情况下趋势延续，可择机介入"
        maxLength={2000}
        showCount
        style={{ marginBottom: 8 }}
      />
      <div style={{ marginBottom: 16 }}>
        <Button type="primary" loading={saving} onClick={submit}>
          入库
        </Button>
      </div>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={items}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: '知识库为空' }}
      />
    </Card>
  );
}
