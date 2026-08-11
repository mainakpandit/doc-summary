import { create } from 'zustand'
import { persist } from 'zustand/middleware'

type ReviewerState = {
  reviewer: string
  setReviewer: (reviewer: string) => void
}

// No auth in this app (CLAUDE.md); reviewer identity is just a name typed
// into the top nav and carried as the `X-Reviewer` header / `reviewer`
// field on requests that need it -- see docs/cuts.md.
export const useReviewerStore = create<ReviewerState>()(
  persist(
    (set) => ({
      reviewer: '',
      setReviewer: (reviewer) => set({ reviewer }),
    }),
    { name: 'reviewer-identity' },
  ),
)
