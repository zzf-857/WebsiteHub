import {
  normalizePortabilityPreview,
  normalizePortabilityResult,
  type PortabilityPreview,
  type PortabilityResult,
} from "./portability-contract.ts";

const BACKEND = "/api/backend";
export const PORTABILITY_EXPORT_URL = `${BACKEND}/portability/export`;

export class PortabilityApiError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "PortabilityApiError";
    this.status = status;
    this.code = code;
  }
}

const ERROR_MESSAGES: Record<string, string> = {
  payload_too_large: "备份超过 64 MiB 上限，请选择较小的备份文件。",
  invalid_format: "备份格式不正确，请选择 WebHub 导出的 JSON 文件。",
  unsupported_version: "此备份版本暂不支持，请升级 WebHub 后重试。",
  limits_exceeded: "备份内容超过数量或字段长度限制，请检查文件后重试。",
  preview_expired: "预览已过期，请重新上传备份。",
  checksum_mismatch: "备份校验不一致，请重新上传并检查预览。",
  not_found: "未找到此预览，请重新上传备份。",
};

function errorDetails(status: number, payload: unknown): { code?: string; message: string } {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const detail = payload.detail;
    if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
      const raw = detail as Record<string, unknown>;
      const code = typeof raw.code === "string" ? raw.code : undefined;
      if (code && Object.hasOwn(ERROR_MESSAGES, code)) {
        return {
          code,
          message: typeof raw.message === "string" && raw.message.trim()
            ? raw.message : ERROR_MESSAGES[code],
        };
      }
    }
  }
  if (status === 413) return { code: "payload_too_large", message: ERROR_MESSAGES.payload_too_large };
  if (status === 401) return { message: "登录已失效，请重新登录后重试。" };
  if (status === 403) return { message: "请求未通过来源校验，请刷新页面后重试。" };
  return { message: "备份操作未完成，请重试。" };
}

async function request(path: string, body: BodyInit, signal?: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${BACKEND}${path}`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body,
      signal,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    throw new PortabilityApiError(0, "网络连接中断，请检查连接后重试。");
  }
  let payload: unknown = null;
  if (response.headers.get("content-type")?.includes("application/json")) {
    try { payload = await response.json(); } catch { /* 非 JSON 的代理错误使用稳定文案。 */ }
  }
  if (!response.ok) {
    const detail = errorDetails(response.status, payload);
    throw new PortabilityApiError(response.status, detail.message, detail.code);
  }
  return payload;
}

export async function uploadPortabilityBackup(file: Blob, signal?: AbortSignal): Promise<PortabilityPreview> {
  return normalizePortabilityPreview(await request("/portability/imports", file, signal));
}

export async function applyPortabilityImport(
  id: string,
  expectedSha256: string,
  signal?: AbortSignal,
): Promise<PortabilityResult> {
  if (!id.trim()) throw new TypeError("预览标识不能为空");
  return normalizePortabilityResult(await request(
    `/portability/imports/${encodeURIComponent(id)}/apply`,
    JSON.stringify({ expected_sha256: expectedSha256 }),
    signal,
  ));
}
