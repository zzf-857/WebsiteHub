// 分类图标白名单（Q52）：分类管理的图标选择器只给这些选项。
// 与后端 services/api/src/webhub/library/icons.py 的 CATEGORY_ICON_NAMES、
// 以及 components/ui/dynamic-icon.tsx 的渲染表保持一致（后端测试会对比三处）。
// 名称是 lucide-react 的导出名；label 用于读屏与悬停提示。

export type CategoryIconOption = Readonly<{ name: string; label: string }>;

export const CATEGORY_ICON_OPTIONS: readonly CategoryIconOption[] = [
  { name: "Folder", label: "文件夹" },
  { name: "Star", label: "星标" },
  { name: "Bookmark", label: "书签" },
  { name: "Library", label: "知识库" },
  { name: "Book", label: "书" },
  { name: "BookOpen", label: "阅读" },
  { name: "FileText", label: "文档" },
  { name: "Newspaper", label: "新闻" },
  { name: "GraduationCap", label: "学习" },
  { name: "Lightbulb", label: "灵感" },
  { name: "Bot", label: "AI" },
  { name: "Cpu", label: "科技" },
  { name: "Code", label: "开发" },
  { name: "Terminal", label: "终端" },
  { name: "Server", label: "后端" },
  { name: "Database", label: "数据库" },
  { name: "Cloud", label: "云" },
  { name: "Monitor", label: "前端" },
  { name: "Shield", label: "安全" },
  { name: "Wrench", label: "工具" },
  { name: "Zap", label: "效率" },
  { name: "Kanban", label: "项目" },
  { name: "Briefcase", label: "工作" },
  { name: "LineChart", label: "财经" },
  { name: "ShoppingCart", label: "购物" },
  { name: "PenTool", label: "设计" },
  { name: "Layout", label: "界面" },
  { name: "LayoutTemplate", label: "原型" },
  { name: "Image", label: "图片" },
  { name: "Camera", label: "摄影" },
  { name: "Video", label: "视频" },
  { name: "Music", label: "音乐" },
  { name: "Gamepad2", label: "娱乐" },
  { name: "Coffee", label: "生活" },
  { name: "Heart", label: "健康" },
  { name: "Users", label: "社交" },
  { name: "User", label: "个人" },
];

const NAMES = new Set(CATEGORY_ICON_OPTIONS.map((option) => option.name));

export function isCategoryIconName(value: string): boolean {
  return NAMES.has(value);
}

/** 当前图标的读屏名；白名单外的旧值按“文件夹”渲染，也按它读。 */
export function categoryIconLabel(name: string): string {
  return CATEGORY_ICON_OPTIONS.find((option) => option.name === name)?.label ?? "文件夹";
}
