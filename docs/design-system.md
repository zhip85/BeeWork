# BeeWork 前端设计规范（统一样式）

所有前端页面共用一套字号、页头、按钮、表单、表格、卡片与弹窗样式。实现位置：

- `frontend/src/index.css`：设计令牌（`:root` 变量）与全局基础样式。
- `frontend/src/design-system.css`：统一组件层，在 `App.css` 之后加载，保证所有页面外观一致。
- `frontend/src/App.css`：各页面的布局样式。**不再写具体字号**，只允许使用 `var(--fs-*)`。

## 1. 字体与字号

字体栈：`Avenir Next, Avenir, SF Pro Text, PingFang SC, Microsoft YaHei, sans-serif`。正文行高 1.5，标题行高 1.25。

| 令牌 | 字号 | 用途 |
| --- | --- | --- |
| `--fs-2xs` | 11px | 小标题（大写）、徽章、说明性标签 |
| `--fs-xs` | 12px | 次要文字、提示、表头、元信息 |
| `--fs-sm` | 13px | 卡片/表格/表单正文、按钮、输入框 |
| `--fs-md` | 14px | 页面基础字号、导航 |
| `--fs-lg` | 16px | 卡片与分区标题（h2） |
| `--fs-xl` | 20px | 弹窗标题 |
| `--fs-2xl` | 24px | 页面标题（h1） |

字重只用 400 / 500 / 600（`--fw-regular|medium|semibold`）。新增 CSS 禁止出现具体 `px` 字号。

## 2. 页面结构

```
[返回链接 .back-link]（仅详情页）
[页头]  小标题 .section-kicker（11px 大写，可选）
        页面标题 h1（24px / 600）
        副标题 p（13px，灰）                      [主操作按钮靠右]
[内容]  卡片 .panel / .detail-card（12px 圆角、1px 边框）
        卡片标题 h2（16px / 600）
```

页头容器可用 `.page-heading`、`.page-header` 或历史类名（`.members-heading`、`.platform-page-heading`、`.welcome-row`），样式一致。同一页面只有一个 h1。

## 3. 控件

- 按钮：高 36px、圆角 8px、13px / 600。`.primary-button`（主色实心）、`.secondary-button`（白底描边）、`.text-button`（无边框，行内操作，高 30px）、`.danger-button`（危险）、`.icon-button`（30px 方形）。
- 输入框 / 下拉 / 多行：高 36px（多行自适应）、圆角 8px、13px；聚焦时主色边框 + 浅绿色光圈；占位符 `#9aa6a1`。字号由设计层强制，组件只控制宽度和内边距。
- 表单标签：12px / 600，说明文字 12px 灰色。
- 搜索框：`.search-field` 外框 + 内部无边框输入。

## 4. 表格与徽章

- 表头 12px / 600 灰色；单元格 13px，行内边距 12px 14px，行间 1px 浅线；次要信息用 `<small>`（12px 灰）。
- 状态徽章 `.assistant-status`、`.member-status` 等：11px / 600，胶囊形。

## 5. 颜色与形状令牌

`--forest`（主色）、`--mint`（浅主色）、`--pollen`（强调）、`--violet`、`--ink` / `--ink-soft` / `--muted`（文字层级）、`--line`（边框）、`--danger`、`--radius-sm|md|lg|xl`（6 / 8 / 12 / 16）、`--control-h`（36）、`--shadow-card`。

## 6. 新页面清单

1. 页头用 h1 + 副标题，不要自己指定字号。
2. 内容放进卡片；卡片标题用 h2。
3. 按钮、输入框直接用原生元素 + 上述类名，不再写 `font-size`。
4. 需要新字号时先讨论是否能用现有令牌；确需新增，在 `index.css` 增加令牌并更新本文。
5. 改动后用 `npm run lint` 和 `npm run build` 检查，并在浏览器里看一遍。
