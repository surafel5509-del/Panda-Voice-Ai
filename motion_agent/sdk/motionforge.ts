export type MotionForgeConfig = {
  baseUrl: string;
  apiKey: string;
  workspaceId: string;
};

export class MotionForgeClient {
  constructor(private readonly config: MotionForgeConfig) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(new URL(path, this.config.baseUrl), {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": this.config.apiKey,
        ...(init.headers ?? {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.detail ?? body.message ?? `MotionForge API error: ${response.status}`);
    }
    return body as T;
  }

  chat(message: string, sessionId?: string) {
    return this.request<{session_id: string; reply: string; model: string; tool_steps: number}>(
      "/api/v1/motion/agent",
      {method: "POST", body: JSON.stringify({
        workspace_id: this.config.workspaceId,
        session_id: sessionId ?? null,
        message,
      })},
    );
  }

  listProjects() {
    return this.request<{projects: unknown[]}>("/api/v1/motion/projects");
  }

  getProject(projectId: string) {
    return this.request<Record<string, unknown>>(
      `/api/v1/motion/projects/${encodeURIComponent(projectId)}`,
    );
  }

  createProject(payload: {title: string; brief: string; movie: Record<string, unknown>}) {
    return this.request<Record<string, unknown>>(
      "/api/v1/motion/projects",
      {method: "POST", body: JSON.stringify(payload)},
    );
  }

  updateProject(projectId: string, payload: Record<string, unknown>) {
    return this.request<Record<string, unknown>>(
      `/api/v1/motion/projects/${encodeURIComponent(projectId)}`,
      {method: "PUT", body: JSON.stringify(payload)},
    );
  }

  renderProject(projectId: string) {
    return this.request<Record<string, unknown>>(
      `/api/v1/motion/projects/${encodeURIComponent(projectId)}/render`,
      {method: "POST"},
    );
  }

  getRender(renderId: string) {
    return this.request<Record<string, unknown>>(
      `/api/v1/motion/renders/${encodeURIComponent(renderId)}`,
    );
  }
}
