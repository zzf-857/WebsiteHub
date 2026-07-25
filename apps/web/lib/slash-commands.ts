export type SlashCommand = {
  name: "/搜索" | "/存入";
  description: string;
  argument: string;
  icon: "search" | "link";
};

export const slashCommands: readonly SlashCommand[] = [
  {
    name: "/搜索",
    description: "检索收藏库，需要时联网",
    argument: "<关键词>",
    icon: "search",
  },
  {
    name: "/存入",
    description: "分析网址并生成收录预览",
    argument: "<URL...>",
    icon: "link",
  },
];

export function suggestSlashCommands(input: string): readonly SlashCommand[] {
  const normalizedInput = input.trimStart();
  if (!normalizedInput.startsWith("/") || /\s/.test(normalizedInput)) {
    return [];
  }

  return slashCommands.filter((command) => command.name.startsWith(normalizedInput));
}
