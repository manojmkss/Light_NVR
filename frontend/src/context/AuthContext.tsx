import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getMe, getSetupStatus, login as loginRequest, runSetup } from "../api/auth";
import { clearTokens, getAccessToken, setTokens } from "../api/client";
import type { User } from "../api/types";

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  isAdmin: boolean;
  setupRequired: boolean;
  setupWizardActive: boolean;
  login: (username: string, password: string) => Promise<void>;
  completeSetup: (username: string, password: string) => Promise<void>;
  finishSetupWizard: () => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [setupRequired, setSetupRequired] = useState(false);
  // Stays true through the rest of the wizard (storage/discovery/notifications)
  // even after the admin account exists and setupRequired has already
  // flipped false, so the routed app doesn't take over mid-wizard.
  const [setupWizardActive, setSetupWizardActive] = useState(false);

  const logout = useCallback(() => {
    clearTokens();
    setUser(null);
  }, []);

  useEffect(() => {
    const handleUnauthorized = () => setUser(null);
    window.addEventListener("lightnvr:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("lightnvr:unauthorized", handleUnauthorized);
  }, []);

  // Independent of the login-state check below, so a stale/invalid token
  // left over from a wiped database doesn't hide the setup wizard.
  useEffect(() => {
    getSetupStatus()
      .then((s) => {
        setSetupRequired(s.setup_required);
        setSetupWizardActive(s.setup_required);
      })
      .catch(() => setSetupRequired(false));
  }, []);

  useEffect(() => {
    if (!getAccessToken()) {
      setLoading(false);
      return;
    }
    getMe()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const tokens = await loginRequest(username, password);
    setTokens(tokens.access_token, tokens.refresh_token);
    const me = await getMe();
    setUser(me);
  }, []);

  const completeSetup = useCallback(async (username: string, password: string) => {
    const tokens = await runSetup(username, password);
    setTokens(tokens.access_token, tokens.refresh_token);
    setSetupRequired(false);
    const me = await getMe();
    setUser(me);
  }, []);

  const finishSetupWizard = useCallback(() => {
    setSetupWizardActive(false);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        isAdmin: user?.role === "admin",
        setupRequired,
        setupWizardActive,
        login,
        completeSetup,
        finishSetupWizard,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
