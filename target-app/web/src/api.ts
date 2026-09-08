export type OrderStatus = "draft" | "placed" | "paid" | "refunded" | "cancelled";
export type Role = "admin" | "member" | "viewer";

export interface OrderItem {
  sku: string;
  description: string;
  quantity: number;
  unit_price_cents: number;
}

export interface Order {
  id: number;
  org_id: number;
  reference: string;
  status: OrderStatus;
  total_cents: number;
  currency: string;
  created_at: string;
  items?: OrderItem[];
}

export interface OrderPage {
  items: Order[];
  total: number;
  limit: number;
  offset: number;
}

export interface OrderCreate {
  reference: string;
  currency?: string;
  items: OrderItem[];
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
  role: Role;
}

export const ORDER_STATUSES: OrderStatus[] = [
  "draft",
  "placed",
  "paid",
  "refunded",
  "cancelled",
];

const TOKEN_KEY = "corvid.token";
const ROLE_KEY = "corvid.role";
const EMAIL_KEY = "corvid.email";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export interface Session {
  token: string;
  role: Role | null;
  email: string | null;
}

function safeStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function getToken(): string | null {
  return safeStorage()?.getItem(TOKEN_KEY) ?? null;
}

export function getSession(): Session | null {
  const token = getToken();
  if (!token) return null;
  const store = safeStorage();
  return {
    token,
    role: (store?.getItem(ROLE_KEY) as Role | null) ?? null,
    email: store?.getItem(EMAIL_KEY) ?? null,
  };
}

export function saveSession(token: string, role: Role, email: string): void {
  const store = safeStorage();
  if (!store) return;
  store.setItem(TOKEN_KEY, token);
  store.setItem(ROLE_KEY, role);
  store.setItem(EMAIL_KEY, email);
}

export function clearSession(): void {
  const store = safeStorage();
  if (!store) return;
  store.removeItem(TOKEN_KEY);
  store.removeItem(ROLE_KEY);
  store.removeItem(EMAIL_KEY);
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string };
  detail?: unknown;
}

async function readError(response: Response): Promise<ApiError> {
  let code = `http_${response.status}`;
  let message = `Request failed with status ${response.status}`;

  try {
    const body = (await response.json()) as ErrorEnvelope;
    if (body && typeof body === "object") {
      if (body.error && typeof body.error === "object") {
        if (typeof body.error.code === "string") code = body.error.code;
        if (typeof body.error.message === "string") message = body.error.message;
      } else if (typeof body.detail === "string") {
        message = body.detail;
      } else if (body.detail != null) {
        message = JSON.stringify(body.detail);
      }
    }
  } catch {
    // Body was not JSON; the status-derived message above is the best we have.
  }

  return new ApiError(response.status, code, message);
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  auth?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, auth = true } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(path, {
    method,
    headers,
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401 && auth) {
    clearSession();
  }

  if (!response.ok) {
    throw await readError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>("/v1/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

export interface ListOrdersParams {
  status?: OrderStatus | "";
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}

export function listOrders(params: ListOrdersParams = {}): Promise<OrderPage> {
  const query = new URLSearchParams();
  if (params.status) query.set("status", params.status);
  if (params.limit != null) query.set("limit", String(params.limit));
  if (params.offset != null) query.set("offset", String(params.offset));

  const suffix = query.toString();
  return request<OrderPage>(`/v1/orders${suffix ? `?${suffix}` : ""}`, {
    signal: params.signal,
  });
}

export function getOrder(orderId: number, signal?: AbortSignal): Promise<Order> {
  return request<Order>(`/v1/orders/${orderId}`, { signal });
}

export function createOrder(payload: OrderCreate): Promise<Order> {
  return request<Order>("/v1/orders", { method: "POST", body: payload });
}

export function formatMoney(cents: number, currency: string): string {
  const amount = (cents ?? 0) / 100;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "USD",
    }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency || ""}`.trim();
  }
}

export function formatDate(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
