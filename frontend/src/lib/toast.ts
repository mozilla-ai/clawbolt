import { addToast } from '@heroui/toast';

export const toast = {
  success: (title: string) => addToast({ title, color: 'success' }),
  error: (title: string) => addToast({ title, color: 'danger' }),
};
