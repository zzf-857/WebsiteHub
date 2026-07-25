import type { Metadata } from "next";

import { AuthForm } from "@/components/auth/auth-form";
import { safeNextPath } from "@/lib/auth-contract";

export const metadata: Metadata = { title: "创建账号 | WebHub" };

type AuthPageProps = {
  searchParams: Promise<{ next?: string | string[] }>;
};

export default async function RegisterPage({ searchParams }: Readonly<AuthPageProps>) {
  const rawNext = (await searchParams).next;
  const nextPath = Array.isArray(rawNext) ? rawNext[0] : rawNext;
  return <AuthForm mode="register" nextPath={nextPath ? safeNextPath(nextPath) : undefined} />;
}
