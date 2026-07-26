"use client";

import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

type SpaceDialogProps = {
  open: boolean;
  title: string;
  description?: string;
  size?: "standard" | "wide";
  children: ReactNode;
  onClose: () => void;
};

export function SpaceDialog({
  open,
  title,
  description,
  size = "standard",
  children,
  onClose,
}: Readonly<SpaceDialogProps>) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      className="space-dialog"
      data-size={size}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (open) onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="space-dialog-surface">
        <header className="space-dialog-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p id={descriptionId}>{description}</p>}
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭" title="关闭">
            <X aria-hidden="true" />
          </button>
        </header>
        <div className="space-dialog-body">{children}</div>
      </div>
    </dialog>
  );
}
