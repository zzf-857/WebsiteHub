"use client";

import { useEffect, useRef, useState } from "react";

import { announceLibraryChanged } from "@/lib/library-events";
import { applyPortabilityImport, PortabilityApiError, uploadPortabilityBackup } from "@/lib/portability-client";
import { portabilityFileError, type PortabilityPreview, type PortabilityResult } from "@/lib/portability-contract";

type Phase = "idle" | "uploading" | "preview" | "applying" | "result";

export function useDataPortability() {
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [preview, setPreview] = useState<PortabilityPreview | null>(null);
  const [result, setResult] = useState<PortabilityResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [invalidPreview, setInvalidPreview] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const requestRef = useRef<{ sequence: number; controller: AbortController | null }>({ sequence: 0, controller: null });
  const busy = phase === "uploading" || phase === "applying";

  useEffect(() => () => {
    requestRef.current.sequence += 1;
    requestRef.current.controller?.abort();
  }, []);

  const selectFile = (nextFile: File | undefined) => {
    if (!nextFile || busy) return;
    requestRef.current.sequence += 1;
    requestRef.current.controller?.abort();
    const validation = portabilityFileError(nextFile);
    setError(validation);
    setFile(validation ? null : nextFile);
    setPreview(null);
    setResult(null);
    setInvalidPreview(false);
    setPhase("idle");
  };

  const beginRequest = () => {
    requestRef.current.controller?.abort();
    const controller = new AbortController();
    const sequence = requestRef.current.sequence + 1;
    requestRef.current = { sequence, controller };
    return { controller, sequence };
  };

  const upload = async () => {
    if (!file || busy) return;
    const { sequence, controller } = beginRequest();
    setPhase("uploading");
    setError(null);
    setPreview(null);
    setResult(null);
    setInvalidPreview(false);
    try {
      const nextPreview = await uploadPortabilityBackup(file, controller.signal);
      if (sequence !== requestRef.current.sequence) return;
      setPreview(nextPreview);
      setPhase("preview");
    } catch (cause) {
      if (sequence !== requestRef.current.sequence) return;
      setPhase("idle");
      setError(cause instanceof Error ? cause.message : "上传失败，请重试。");
    }
  };

  const apply = async () => {
    if (!preview || busy || invalidPreview) return;
    const { sequence, controller } = beginRequest();
    setConfirming(false);
    setPhase("applying");
    setError(null);
    try {
      const nextResult = await applyPortabilityImport(preview.id, preview.payloadSha256, controller.signal);
      if (sequence !== requestRef.current.sequence) return;
      setResult(nextResult);
      setPhase("result");
      announceLibraryChanged();
    } catch (cause) {
      if (sequence !== requestRef.current.sequence) return;
      setPhase("preview");
      setError(cause instanceof Error ? cause.message : "导入失败，请重试。");
      setInvalidPreview(cause instanceof PortabilityApiError
        && ["preview_expired", "checksum_mismatch", "not_found"].includes(cause.code ?? ""));
    }
  };

  return {
    file, phase, preview, result, error, busy, invalidPreview, confirming,
    selectFile, upload, apply, setConfirming,
  };
}
