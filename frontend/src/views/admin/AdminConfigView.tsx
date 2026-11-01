import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import {
  getConfig,
  testConnectivity,
  updateConfig,
} from "../../api/admin";
import type {
  ConfigField,
  ConfigResponse,
  ConnectivityResult,
} from "../../api/types";

const IMPL_KEYS = new Set(["asr_impl", "llm_impl", "streaming_asr_impl"]);

function origText(field: ConfigField): string {
  return field.value === null || field.value === undefined
    ? ""
    : String(field.value);
}

export function AdminConfigView() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [clearSecret, setClearSecret] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conn, setConn] = useState<ConnectivityResult | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const c = await getConfig();
      setConfig(c);
      const initial: Record<string, string> = {};
      for (const f of c.editable) {
        if (!f.secret) initial[f.key] = origText(f);
        else initial[f.key] = "";
      }
      setEdits(initial);
      setClearSecret(new Set());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "加载配置失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onSave = async () => {
    if (!config) return;
    setError(null);
    setNotice(null);
    const changes: Record<string, unknown> = {};
    for (const f of config.editable) {
      if (f.secret) {
        if (clearSecret.has(f.key)) changes[f.key] = null;
        else if (edits[f.key] !== "") changes[f.key] = edits[f.key];
      } else {
        const orig = origText(f);
        const cur = edits[f.key] ?? "";
        if (cur === orig) continue;
        changes[f.key] = cur === "" ? null : cur;
      }
    }
    if (Object.keys(changes).length === 0) {
      setNotice("没有需要保存的更改");
      return;
    }
    setBusy(true);
    try {
      const updated = await updateConfig(changes);
      setConfig(updated);
      const initial: Record<string, string> = {};
      for (const f of updated.editable)
        initial[f.key] = f.secret ? "" : origText(f);
      setEdits(initial);
      setClearSecret(new Set());
      setNotice("配置已保存");
    } catch (err) {
      if (err instanceof ApiError) {
        const errs = (err.body as { errors?: { key: string; problem: string }[] })
          ?.errors;
        setError(
          errs && errs.length
            ? `配置无效：${errs.map((e) => `${e.key} (${e.problem})`).join("；")}`
            : err.detail || err.message,
        );
      } else {
        setError("保存失败");
      }
    } finally {
      setBusy(false);
    }
  };

  const onTest = async () => {
    setError(null);
    setConn(null);
    setBusy(true);
    try {
      setConn(await testConnectivity());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "连通性测试失败");
    } finally {
      setBusy(false);
    }
  };

  if (!config) {
    return (
      <div className="card">
        {error ? <div className="alert alert--error">{error}</div> : "加载中…"}
      </div>
    );
  }

  return (
    <div className="card" data-testid="admin-config">
      <h2>模型配置</h2>
      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}
      {notice && <div className="alert alert--info">{notice}</div>}

      <div className="admin-config__fields">
        {config.editable.map((f) => (
          <div className="field" key={f.key}>
            <label className="field__label" htmlFor={`cfg-${f.key}`}>
              {f.key}{" "}
              <span className="badge muted" data-testid={`source-${f.key}`}>
                {f.source === "runtime" ? "运行时覆盖" : "环境默认"}
              </span>
            </label>

            {f.secret ? (
              <div className="admin-config__secret">
                <span
                  className="muted"
                  data-testid={`secret-state-${f.key}`}
                >
                  {f.configured ? "已配置" : "未配置"}
                </span>
                <input
                  id={`cfg-${f.key}`}
                  className="input"
                  type="password"
                  placeholder="留空表示不修改"
                  data-testid={`input-${f.key}`}
                  value={edits[f.key] ?? ""}
                  disabled={clearSecret.has(f.key)}
                  onChange={(e) =>
                    setEdits((s) => ({ ...s, [f.key]: e.target.value }))
                  }
                />
                {f.configured && (
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      data-testid={`clear-${f.key}`}
                      checked={clearSecret.has(f.key)}
                      onChange={(e) =>
                        setClearSecret((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(f.key);
                          else next.delete(f.key);
                          return next;
                        })
                      }
                    />
                    清除
                  </label>
                )}
              </div>
            ) : IMPL_KEYS.has(f.key) ? (
              <select
                id={`cfg-${f.key}`}
                className="input"
                data-testid={`input-${f.key}`}
                value={edits[f.key] ?? ""}
                onChange={(e) =>
                  setEdits((s) => ({ ...s, [f.key]: e.target.value }))
                }
              >
                <option value="mock">mock</option>
                <option value="http">http</option>
              </select>
            ) : (
              <input
                id={`cfg-${f.key}`}
                className="input"
                type="text"
                data-testid={`input-${f.key}`}
                value={edits[f.key] ?? ""}
                onChange={(e) =>
                  setEdits((s) => ({ ...s, [f.key]: e.target.value }))
                }
              />
            )}
          </div>
        ))}
      </div>

      <div className="admin-config__actions">
        <button
          type="button"
          className="btn btn--primary"
          data-testid="config-save"
          onClick={onSave}
          disabled={busy}
        >
          保存
        </button>
        <button
          type="button"
          className="btn"
          data-testid="config-test"
          onClick={onTest}
          disabled={busy}
        >
          测试连接
        </button>
      </div>

      {conn && (
        <div className="admin-config__conn" data-testid="config-conn">
          <div data-testid="conn-asr">
            ASR: {conn.asr.status} — {conn.asr.message}
          </div>
          <div data-testid="conn-llm">
            LLM: {conn.llm.status} — {conn.llm.message}
          </div>
        </div>
      )}

      <h3>只读（需重启）</h3>
      <ul className="admin-config__readonly">
        {config.restart_only.map((f) => (
          <li key={f.key}>
            <code>{f.key}</code>:{" "}
            {f.secret
              ? f.configured
                ? "已配置"
                : "未配置"
              : String(f.value ?? "")}
          </li>
        ))}
      </ul>
    </div>
  );
}
