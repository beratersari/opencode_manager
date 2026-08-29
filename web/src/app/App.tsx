import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { JobDetailPage } from '../pages/jobs/JobDetailPage'
import { JobsPage } from '../pages/jobs/JobsPage'
import { LiveProvider } from './LiveProvider'
import { Shell } from './Shell'

export default function App() {
  return (
    <BrowserRouter>
      <LiveProvider>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/" element={<Navigate to="/jobs" replace />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/jobs/:jobId" element={<JobDetailPage />} />
            <Route path="*" element={<Navigate to="/jobs" replace />} />
          </Route>
        </Routes>
      </LiveProvider>
    </BrowserRouter>
  )
}
