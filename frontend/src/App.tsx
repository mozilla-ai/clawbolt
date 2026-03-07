import { Routes, Route } from 'react-router-dom';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<div className="flex items-center justify-center min-h-screen"><h1 className="text-2xl font-bold text-foreground">Clawbolt</h1></div>} />
    </Routes>
  );
}
