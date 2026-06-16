import { configureStore } from "@reduxjs/toolkit";
import { useDispatch, useSelector, type TypedUseSelectorHook } from "react-redux";
import { runsApi } from "../api/runsApi";
import { authApi } from "../api/authApi";
import runFormReducer from "../features/runForm/runFormSlice";
import runStreamReducer from "../features/runStream/runStreamSlice";
import viewerReducer from "../features/viewer/viewerSlice";

export const store = configureStore({
  reducer: {
    [runsApi.reducerPath]: runsApi.reducer,
    [authApi.reducerPath]: authApi.reducer,
    runForm: runFormReducer,
    runStream: runStreamReducer,
    viewer: viewerReducer,
  },
  middleware: (getDefault) => getDefault().concat(runsApi.middleware, authApi.middleware),
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;

export const useAppDispatch = () => useDispatch<AppDispatch>();
export const useAppSelector: TypedUseSelectorHook<RootState> = useSelector;
