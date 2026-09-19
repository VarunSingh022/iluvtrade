import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ApiError, api } from "./api";
import type { User } from "./api";

interface AuthValue {
  user: User | undefined;
  loading: boolean;
  /** Throws `MfaChallengeRequired` when the account needs a second factor. */
  login: (email: string, password: string, mfaCode?: string) => Promise<void>;
  register: (email: string, password: string, displayName: string, organizationName?: string) => Promise<void>;
  logout: () => Promise<void>;
  switchOrganization: (organizationId: string) => Promise<void>;
  refresh: () => Promise<void>;
}

/**
 * The password was right; a verification code is still needed.
 *
 * A distinct type rather than a flag, so a caller that forgets to handle it
 * cannot accidentally render it as "invalid email or password" — which would
 * tell someone with a perfectly good password that it is wrong, and leave the
 * account unreachable through the app.
 */
export class MfaChallengeRequired extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MfaChallengeRequired";
  }
}

/** Whether an error is the server asking for a second factor. */
export function isMfaChallenge(error: unknown): boolean {
  if (error instanceof MfaChallengeRequired) return true;
  return error instanceof ApiError && error.code === "MfaRequired";
}

const AuthContext = createContext<AuthValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | undefined>(undefined);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setUser(await api.get<User>("/auth/me"));
    } catch (error) {
      // A 401 here is the ordinary "not signed in" case, not a failure.
      if (!(error instanceof ApiError && error.isUnauthenticated)) throw error;
      setUser(undefined);
    }
  }, []);

  useEffect(() => {
    void refresh().finally(() => setLoading(false));
  }, [refresh]);

  /**
   * Drop the signed-in user whenever any call reports the session is gone.
   *
   * Without this a session that expired mid-visit leaves the shell rendered
   * around screens that each show their own 401, and nothing sends the user
   * back to sign in. One listener here turns that into the login screen.
   */
  useEffect(() => {
    const onExpired = () => setUser(undefined);
    window.addEventListener("iluvtrade:session-expired", onExpired);
    return () => window.removeEventListener("iluvtrade:session-expired", onExpired);
  }, []);

  const value = useMemo<AuthValue>(
    () => ({
      user,
      loading,
      login: async (email, password, mfaCode) => {
        try {
          const session = await api.post<{ user: User }>("/auth/login", {
            email,
            password,
            ...(mfaCode ? { mfa_code: mfaCode } : {}),
          });
          setUser(session.user);
        } catch (error) {
          if (error instanceof ApiError && error.code === "MfaRequired") {
            throw new MfaChallengeRequired(error.message);
          }
          throw error;
        }
      },
      register: async (email, password, displayName, organizationName) => {
        const session = await api.post<{ user: User }>("/auth/register", {
          email,
          password,
          display_name: displayName,
          organization_name: organizationName || null,
        });
        setUser(session.user);
      },
      logout: async () => {
        await api.post("/auth/logout");
        setUser(undefined);
      },
      switchOrganization: async (organizationId) => {
        const session = await api.post<{ user: User }>("/auth/switch-organization", {
          organization_id: organizationId,
        });
        setUser(session.user);
      },
      refresh,
    }),
    [user, loading, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
