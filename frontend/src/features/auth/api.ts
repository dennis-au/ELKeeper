import { api, getToken, setToken } from '../../shared/api';

export interface LoginResponse { token: string; username: string }

export const authApi = {
  login: (username: string, password: string) => api<LoginResponse>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  token: getToken,
  setToken,
};

export function notifyAuthExpired() {
  window.dispatchEvent(new Event('elastic-auth-expired'));
}
