import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Database, LoaderCircle } from 'lucide-react';
import { acceptAccess, announceLogin, api, authChangedEvent, clearAccess, message } from './api';
import type { AuthSession, Identity } from './types';

const boundaryFallback =
  '智能查询会将用户原文、显式引用的已确认业务知识、获准用于模型的表结构、SQL 与计划摘要发给配置模型；查询结果行不发送给模型。直接 SQL 查询与诊断不调用模型。';

export default function AuthGate({
  children,
}: {
  children: (
    identity: Identity,
    boundary: string,
    logout: () => void,
    accessMode: 'local' | 'password',
  ) => ReactNode;
}) {
  const [session, setSession] = useState<AuthSession | null>(null);
  const sessionRef = useRef<AuthSession | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [boundary, setBoundary] = useState(boundaryFallback);
  const [accessMode, setAccessMode] = useState<'local' | 'password' | null>(null);
  const checkingRef = useRef(false);
  const verify = useCallback(async () => {
    if (checkingRef.current) return;
    checkingRef.current = true;
    try {
      const next = await api<AuthSession>('/auth/session');
      const previous = sessionRef.current;
      setAccessMode(next.access_mode || 'password');
      if (
        previous?.authenticated &&
        (!next.authenticated ||
          previous.session_id !== next.session_id ||
          previous.access_mode !== next.access_mode ||
          JSON.stringify(previous.identity) !== JSON.stringify(next.identity))
      ) {
        clearAccess(
          next.access_mode === 'local'
            ? '工作区连接已失效或配置已变更，请重新连接。'
            : '登录已失效或身份权限已变更，请重新登录。',
        );
        return;
      }
      if (next.model_boundary) setBoundary(next.model_boundary);
      if (next.authenticated && next.identity && next.session_id) {
        acceptAccess(next.session_id);
        sessionRef.current = next;
        setSession(next);
        setError('');
      }
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      clearAccess(`无法连接本机工作区：${message(cause)}`);
    } finally {
      checkingRef.current = false;
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    const invalidated = (event: Event) => {
      sessionRef.current = null;
      setSession(null);
      setChecking(false);
      setUsername('');
      setPassword('');
      setError((event as CustomEvent<string>).detail);
    };
    window.addEventListener(authChangedEvent, invalidated);
    void verify();
    return () => window.removeEventListener(authChangedEvent, invalidated);
  }, [verify]);

  useEffect(() => {
    if (!session?.authenticated) return;
    const resume = () => {
      if (document.visibilityState === 'visible') void verify();
    };
    const timer = setInterval(resume, 15_000);
    window.addEventListener('focus', resume);
    document.addEventListener('visibilitychange', resume);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, [session?.authenticated, verify]);

  async function login(event: FormEvent) {
    event.preventDefault();
    if (busy || checking) return;
    setBusy(true);
    setError('');
    const submittedPassword = password;
    setPassword('');
    try {
      const next = await api<AuthSession>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password: submittedPassword }),
      });
      if (!next.authenticated || !next.identity || !next.session_id)
        throw new Error('服务未返回有效登录身份。');
      if (next.model_boundary) setBoundary(next.model_boundary);
      acceptAccess(next.session_id);
      sessionRef.current = next;
      setSession(next);
      setAccessMode(next.access_mode || 'password');
      announceLogin();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function logout() {
    clearAccess('已清空当前页面，请登录后继续。');
    setBusy(true);
    try {
      await api('/auth/logout', { method: 'POST', body: '{}' });
    } catch (cause) {
      setError(`页面内容已清空，但服务端退出尚未确认：${message(cause)}`);
    } finally {
      setBusy(false);
    }
  }

  if (session?.authenticated && session.identity)
    return children(
      session.identity,
      boundary,
      () => void logout(),
      session.access_mode || 'password',
    );

  const passwordMode = accessMode === 'password';

  return (
    <main className="auth-page">
      <section
        className="auth-card"
        aria-label={passwordMode ? '登录本机工作区' : '连接本机工作区'}
      >
        <div className="auth-brand">
          <span className="brand-icon">
            <Database size={23} />
          </span>
          <span>DB Agent</span>
        </div>
        <h1>{passwordMode ? '登录本机工作区' : '连接本机工作区'}</h1>
        <p className="muted">
          {passwordMode
            ? '本机隔离合成环境。使用管理员配置的身份访问获准数据。'
            : '连接本机服务后即可查询数据和诊断 SQL。'}
        </p>
        {checking ? (
          <div className="panel-loading">
            <LoaderCircle className="spin" size={20} />
            正在连接工作区…
          </div>
        ) : passwordMode ? (
          <form onSubmit={login}>
            <label className="field-label">
              用户名
              <input
                value={username}
                autoComplete="username"
                maxLength={128}
                required
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>
            <label className="field-label">
              密码
              <input
                type="password"
                value={password}
                autoComplete="current-password"
                maxLength={1024}
                required
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            {error && (
              <div className="notice error" role="alert">
                {error}
              </div>
            )}
            <button
              className="primary-button auth-submit"
              type="submit"
              disabled={busy || !username.trim() || !password}
            >
              {busy && <LoaderCircle className="spin" size={16} />}登录
            </button>
          </form>
        ) : (
          <>
            {error && (
              <div className="notice error" role="alert">
                {error}
              </div>
            )}
            <button
              className="primary-button auth-submit"
              onClick={() => {
                setChecking(true);
                setError('');
                void verify();
              }}
            >
              重新连接工作区
            </button>
          </>
        )}
        <div className="auth-boundary">
          <strong>模型数据使用范围</strong>
          <p>{boundary}</p>
          <p>智能查询使用本机服务配置的数据表和模型；直接 SQL 查询与诊断不调用模型。</p>
        </div>
      </section>
    </main>
  );
}
