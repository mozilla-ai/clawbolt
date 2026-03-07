/** OSS stub: premium overlay replaces with real admin API calls. */

export interface AdminContractor {
  id: number;
  name: string;
  user_id: string;
  is_active: boolean;
  created_at: string;
}

export async function getAdminContractors(): Promise<AdminContractor[]> {
  throw new Error('Not available in OSS');
}
