import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// AI 回复的 Markdown 渲染组件。
// remark-gfm 用于支持表格、删除线等 GFM 扩展语法。
// 外层包一个 .md 容器，方便在样式表里对渲染出的元素统一施加样式。
export default function Markdown({ children }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  )
}
