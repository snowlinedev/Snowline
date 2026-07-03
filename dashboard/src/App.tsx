import { Route, Routes } from "react-router-dom";

import { Home } from "./pages/Home";
import { Plugins } from "./pages/Plugins";
import { Scopes } from "./pages/Scopes";
import { Surfaces } from "./pages/Surfaces";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/plugins" element={<Plugins />} />
      <Route path="/surfaces" element={<Surfaces />} />
      <Route path="/scopes" element={<Scopes />} />
    </Routes>
  );
}
