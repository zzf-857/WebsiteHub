"use client";

import { ArrowRight, Eye, EyeOff } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { useAuth } from "@/components/auth/auth-context";
import { Spinner } from "@/components/react-bits/spinner";
import { submitCredentials } from "@/lib/auth-client";
import {
  safeNextPath,
  validateAuthForm,
  type AuthFieldErrors,
  type AuthFormValues,
  type AuthMode,
} from "@/lib/auth-contract";

const initialValues: AuthFormValues = {
  username: "",
  password: "",
  confirmPassword: "",
  displayName: "",
};

export function AuthForm({
  mode,
  nextPath,
}: Readonly<{ mode: AuthMode; nextPath?: string }>) {
  const isRegister = mode === "register";
  const router = useRouter();
  const auth = useAuth();
  const [values, setValues] = useState(initialValues);
  const [fieldErrors, setFieldErrors] = useState<AuthFieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [passwordVisible, setPasswordVisible] = useState(false);

  const updateField = (field: keyof AuthFormValues, value: string) => {
    setValues((current) => ({ ...current, [field]: value }));
    setFieldErrors((current) => ({ ...current, [field]: undefined }));
    setFormError(null);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const errors = validateAuthForm(mode, values);
    setFieldErrors(errors);
    setFormError(null);
    if (Object.keys(errors).length > 0) return;

    setSubmitting(true);
    try {
      const displayName = values.displayName.trim();
      const user = await submitCredentials(mode, {
        username: values.username.trim(),
        password: values.password,
        ...(isRegister && displayName ? { display_name: displayName } : {}),
      });
      auth.establishSession(user);
      router.replace(safeNextPath(nextPath));
      router.refresh();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "提交失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-main">
      <section className="auth-panel" aria-labelledby="auth-title">
        <header className="auth-panel-header">
          <span className="auth-kicker">WebHub</span>
          <h1 id="auth-title">{isRegister ? "创建账号" : "登录"}</h1>
          <p>{isRegister ? "建立你的独立工作区。" : "进入你的 WebHub 工作区。"}</p>
        </header>

        <form className="auth-form" onSubmit={handleSubmit} noValidate>
          {isRegister && (
            <div className="auth-field">
              <label htmlFor="display-name">显示名称 <span>选填</span></label>
              <input
                id="display-name"
                name="display_name"
                autoComplete="name"
                value={values.displayName}
                onChange={(event) => updateField("displayName", event.target.value)}
                aria-invalid={Boolean(fieldErrors.displayName)}
                aria-describedby={fieldErrors.displayName ? "display-name-error" : undefined}
                disabled={submitting}
              />
              {fieldErrors.displayName && (
                <p className="field-error" id="display-name-error">{fieldErrors.displayName}</p>
              )}
            </div>
          )}

          <div className="auth-field">
            <label htmlFor="username">用户名</label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              value={values.username}
              onChange={(event) => updateField("username", event.target.value)}
              aria-invalid={Boolean(fieldErrors.username)}
              aria-describedby={fieldErrors.username ? "username-error" : undefined}
              disabled={submitting}
              autoFocus
              required
            />
            {fieldErrors.username && (
              <p className="field-error" id="username-error">{fieldErrors.username}</p>
            )}
          </div>

          <div className="auth-field">
            <label htmlFor="password">密码</label>
            <div className="password-input">
              <input
                id="password"
                name="password"
                type={passwordVisible ? "text" : "password"}
                autoComplete={isRegister ? "new-password" : "current-password"}
                value={values.password}
                onChange={(event) => updateField("password", event.target.value)}
                aria-invalid={Boolean(fieldErrors.password)}
                aria-describedby={fieldErrors.password ? "password-error" : undefined}
                disabled={submitting}
                required
              />
              <button
                type="button"
                onClick={() => setPasswordVisible((visible) => !visible)}
                aria-label={passwordVisible ? "隐藏密码" : "显示密码"}
                title={passwordVisible ? "隐藏密码" : "显示密码"}
                disabled={submitting}
              >
                {passwordVisible ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
              </button>
            </div>
            {fieldErrors.password && (
              <p className="field-error" id="password-error">{fieldErrors.password}</p>
            )}
          </div>

          {isRegister && (
            <div className="auth-field">
              <label htmlFor="confirm-password">确认密码</label>
              <input
                id="confirm-password"
                name="confirm_password"
                type={passwordVisible ? "text" : "password"}
                autoComplete="new-password"
                value={values.confirmPassword}
                onChange={(event) => updateField("confirmPassword", event.target.value)}
                aria-invalid={Boolean(fieldErrors.confirmPassword)}
                aria-describedby={fieldErrors.confirmPassword ? "confirm-password-error" : undefined}
                disabled={submitting}
                required
              />
              {fieldErrors.confirmPassword && (
                <p className="field-error" id="confirm-password-error">{fieldErrors.confirmPassword}</p>
              )}
            </div>
          )}

          {formError && <p className="auth-error" role="alert">{formError}</p>}

          <button className="auth-submit" type="submit" disabled={submitting}>
            {submitting ? (
              <Spinner />
            ) : (
              <ArrowRight aria-hidden="true" />
            )}
            <span>{submitting ? "正在提交" : isRegister ? "创建并登录" : "登录"}</span>
          </button>
        </form>

        <p className="auth-switch">
          {isRegister ? "已有账号？" : "第一次使用？"}
          <Link
            href={
              nextPath
                ? `${isRegister ? "/login" : "/register"}?next=${encodeURIComponent(safeNextPath(nextPath))}`
                : isRegister ? "/login" : "/register"
            }
          >
            {isRegister ? "返回登录" : "创建账号"}
          </Link>
        </p>
      </section>
    </main>
  );
}
