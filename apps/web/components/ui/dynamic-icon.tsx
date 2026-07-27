import {
  Book, BookOpen, Bookmark, Bot, Briefcase, Camera, Cloud, Code, Coffee,
  Cpu, Database, FileText, Folder, Gamepad2, GraduationCap,
  Heart, Image, Kanban, Layout, LayoutTemplate, Library, Lightbulb,
  LineChart, Monitor, Music, Newspaper, PenTool, Server, Shield,
  ShoppingCart, Star, Terminal, User, Users, Video, Wrench, Zap
} from "lucide-react";
import React from "react";
import type { LucideProps } from "lucide-react";

const ICON_MAP: Record<string, React.FC<LucideProps>> = {
  Book, BookOpen, Bookmark, Bot, Briefcase, Camera, Cloud, Code, Coffee,
  Cpu, Database, FileText, Folder, Gamepad2, GraduationCap,
  Heart, Image, Kanban, Layout, LayoutTemplate, Library, Lightbulb,
  LineChart, Monitor, Music, Newspaper, PenTool, Server, Shield,
  ShoppingCart, Star, Terminal, User, Users, Video, Wrench, Zap
};

export type DynamicIconProps = Omit<LucideProps, "ref"> & {
  name: string;
};

export const DynamicIcon = React.memo(({ name, ...props }: DynamicIconProps) => {
  const IconComponent = ICON_MAP[name] || Folder;
  return <IconComponent {...props} />;
});
DynamicIcon.displayName = "DynamicIcon";
