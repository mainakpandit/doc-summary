import { create } from 'zustand'

export type ToastVariant = 'default' | 'destructive'

export type Toast = {
  id: number
  title: string
  description?: string
  variant?: ToastVariant
}

type ToastState = {
  toasts: Toast[]
  dismiss: (id: number) => void
}

const TOAST_DURATION_MS = 5000

let nextId = 1

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  dismiss: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}))

export function toast(input: Omit<Toast, 'id'>): void {
  const id = nextId++
  useToastStore.setState((state) => ({ toasts: [...state.toasts, { ...input, id }] }))
  setTimeout(() => {
    useToastStore.getState().dismiss(id)
  }, TOAST_DURATION_MS)
}
