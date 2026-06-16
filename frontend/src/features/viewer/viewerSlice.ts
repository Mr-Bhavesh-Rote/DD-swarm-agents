// viewerSlice (§8.2): active tab, active section, active citation.
import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

export type ViewerTab = "final" | "raw" | "sources";

interface ViewerState {
  tab: ViewerTab;
  activeSection: string | null;
  activeCitation: number | null;
}

const initialState: ViewerState = { tab: "final", activeSection: null, activeCitation: null };

const viewerSlice = createSlice({
  name: "viewer",
  initialState,
  reducers: {
    setTab(state, action: PayloadAction<ViewerTab>) {
      state.tab = action.payload;
    },
    setActiveSection(state, action: PayloadAction<string | null>) {
      state.activeSection = action.payload;
    },
    setActiveCitation(state, action: PayloadAction<number | null>) {
      state.activeCitation = action.payload;
    },
  },
});

export const { setTab, setActiveSection, setActiveCitation } = viewerSlice.actions;
export default viewerSlice.reducer;
