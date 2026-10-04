export type IconName = 'phone' | 'settings' | 'history' | 'arrow' | 'check' | 'mic' | 'muted' | 'end';
const paths: Record<IconName, string> = {
  phone: 'M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.2 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.69 2.79a2 2 0 0 1-.45 2.11L8.09 9.89a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.89.33 1.83.56 2.79.69A2 2 0 0 1 22 16.92Z',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm-9-1 3 1-1-3 3-2 2 3 2-1 2 1 2-3 3 2-1 3 3-1 1 4-3 1 2 2-2 3-3-1-1 3H9l-1-3-3 1-2-3 2-2-3-1 1-4Z',
  history: 'M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5m4-1v5l3 2',
  arrow: 'M5 12h14m-5-5 5 5-5 5',
  check: 'm5 12 4 4L19 6',
  mic: 'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Zm-7 8v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8',
  muted: 'm2 2 20 20M9 9v3a3 3 0 0 0 5 2M9 5a3 3 0 0 1 6 0v5m-10 0v2a7 7 0 0 0 12 5m2-7v2M12 19v3m-4 0h8',
  end: 'M4 15a2 2 0 0 1-2-2v-2c5-6 15-6 20 0v2a2 2 0 0 1-2 2h-2a2 2 0 0 1-2-2v-2a14 14 0 0 0-8 0v2a2 2 0 0 1-2 2H4Z',
};

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
