import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ApiError, api } from "./api";
import type { User } from "./api";

interface AuthValue {
  user: User | undefined;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName: string, organizationName?: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
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

  const value = useMemo<AuthValue>(
    () => ({
      user,
      loading,
      login: async (email, password) => {
        const session = await api.post<{ user: User }>("/auth/login", { email, password });
        setUser(session.user);
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
