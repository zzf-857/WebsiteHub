import type { Metadata } from "next";

import { AuthForm } from "@/components/auth/auth-form";
import { safeNextPath } from "@/lib/auth-contract";

export const metadata: Metadata = { title: "登录 | WebHub" };

type AuthPageProps = {
  searchParams: Promise<{ next?: string | string[] }>;
};

export default async function LoginPage({ searchParams }: Readonly<AuthPageProps>) {
  const rawNext = (await searchParams).next;
  const nextPath = Array.isArray(rawNext) ? rawNext[0] : rawNext;
  return <AuthForm mode="login" nextPath={nextPath ? safeNextPath(nextPath) : undefined} />;
}
