/** OSS stub: premium overlay replaces with real admin API calls. */

export interface AdminUser {
  id: number;
  user_id: string;
  is_active: boolean;
  created_at: string;
}

export async function getAdminUsers(): Promise<AdminUser[]> {
  throw new Error('Not available in OSS');
}
