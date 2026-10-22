"use client";

import { Link2, Plus } from "lucide-react";
import { usePathname } from "next/navigation";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { useToast } from "@/components/ui/toast";
import { createLibrarySite } from "@/lib/library-client";
import { announceLibraryChanged } from "@/lib/library-events";
import {
  duplicateSiteHref,
  isDuplicateSiteError,
  parseQuickAddUrl,
  siteDetailHref,
} from "@/lib/quick-add";

// 快速收藏（Q44）：顶栏“收藏”浮层与首次使用面板共用同一个表单。
// 名称先用主机名，创建后后端自动排队整理（类型、标签、简介、封面）。

type QuickAddFormProps = {
  variant: "popover" | "panel";
  autoFocus?: boolean;
  onDone?: () => void;
};

export function QuickAddForm({ variant, autoFocus = false, onDone }: Readonly<QuickAddFormProps>) {
  const id = useId();
  const inputId = `${id}-url`;
  const errorId = `${id}-error`;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const parsed = parseQuickAddUrl(value);
    if (!parsed.ok) {
      setError(parsed.error);
      inputRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const site = await createLibrarySite({ name: parsed.name, url: parsed.url });
      setValue("");
      announceLibraryChanged();
      toast.show({ message: "已收藏，正在整理…", link: { label: "查看", href: siteDetailHref(site.id) } });
      onDone?.();
    } catch (failure) {
      if (isDuplicateSiteError(failure)) {
        setValue("");
        toast.show({ message: "已经收藏过了", link: { label: "去看看", href: duplicateSiteHref(parsed.url) } });
        onDone?.();
      } else {
        setError(failure instanceof Error && failure.message.trim() ? failure.message : "收藏失败，请稍后重试");
        inputRef.current?.focus();
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="quick-add-form" data-variant={variant} onSubmit={(event) => void submit(event)} noValidate>
      <label className="sr-only" htmlFor={inputId}>
        要收藏的网址
      </label>
      <div className="quick-add-field" data-invalid={error ? true : undefined}>
        <Link2 className="quick-add-field-icon" aria-hidden="true" />
        <input
          ref={inputRef}
          id={inputId}
          className="quick-add-input"
          type="url"
          inputMode="url"
          placeholder="粘贴网址，回车收藏"
          autoComplete="off"
          spellCheck={false}
          value={value}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(event) => {
            setValue(event.target.value);
            if (error) setError(null);
          }}
        />
        <button className="quick-add-submit" type="submit" disabled={busy}>
          {busy && <Spinner size={14} />}
          {busy ? "收藏中" : "收藏"}
        </button>
      </div>
      {error && (
        <p className="quick-add-error" id={errorId} role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

/** 顶栏的“收藏”按钮 + 小浮层。Esc、点外部或跳转路由都会收起。 */
export function QuickAddButton() {
  const pathname = usePathname();
  const popoverId = useId();
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  // 记下是在哪个路由打开的：路由一变自然视为关闭，不需要在 effect 里同步 setState
  const [openPath, setOpenPath] = useState<string | null>(null);
  const open = openPath === pathname;

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && wrapperRef.current?.contains(event.target)) return;
      setOpenPath(null);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpenPath(null);
      triggerRef.current?.focus();
    };
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div className="quick-add" ref={wrapperRef}>
      <button
        ref={triggerRef}
        className="quick-add-trigger"
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        aria-label="收藏一个网址"
        onClick={() => setOpenPath(open ? null : pathname)}
      >
        <Plus aria-hidden="true" />
        <span className="quick-add-trigger-label">收藏</span>
      </button>
      {open && (
        <div className="quick-add-popover" id={popoverId} role="dialog" aria-label="快速收藏">
          <p className="quick-add-hint">粘贴网址即可，类型、标签和简介会自动整理。</p>
          <QuickAddForm
            variant="popover"
            autoFocus
            onDone={() => {
              setOpenPath(null);
              triggerRef.current?.focus();
            }}
          />
        </div>
      )}
    </div>
  );
}
