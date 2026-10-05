import { Link, Navigate, Route, Routes } from "react-router-dom";
import { NAMED_TYPES } from "./api";
import { Page, Shell } from "./components/Shell";
import { Empty, Loading } from "./components/ui";
import { Allowlist } from "./pages/admin/Allowlist";
import { Audit } from "./pages/admin/Audit";
import { Clusters } from "./pages/admin/Clusters";
import { Users } from "./pages/admin/Users";
import { ChangePassword } from "./pages/ChangePassword";
import { ClusterSettings } from "./pages/ClusterSettings";
import { Data } from "./pages/Data";
import { IndexDetail } from "./pages/IndexDetail";
import { Indices } from "./pages/Indices";
import { Login } from "./pages/Login";
import { NamedResources } from "./pages/NamedResources";
import { Overview } from "./pages/Overview";
import { RequestDetail, Requests } from "./pages/Requests";
import { ShellPage } from "./pages/ShellPage";
import { lastCluster, RequireAdmin, RequireAuth, useClusters, useMe } from "./session";

function Home() {
  const clusters = useClusters();
  const me = useMe().data!;
  if (clusters.isLoading) return <Loading />;
  const items = clusters.data ?? [];
  const remembered = lastCluster();
  const target = items.find((c) => c.id === remembered) ?? items[0];
  if (target) return <Navigate to={`/c/${encodeURIComponent(target.id)}`} replace />;
  return (
    <Page crumbs={[{ label: "Home" }]} title="No clusters">
      <section className="card">
        <Empty title="You don't have access to any cluster yet">
          {me.admin ? <><Link to="/admin/clusters">Add a cluster</Link> in Administration, or list it in <code>config/clusters.yaml</code> on the server.</> : "Ask an admin to give you access."}
        </Empty>
      </section>
    </Page>
  );
}

function NotFound() {
  return (
    <Page crumbs={[{ label: "Not found" }]} title="Not found">
      <section className="card">
        <Empty title="This page doesn't exist"><Link to="/">Go to the overview</Link></Empty>
      </section>
    </Page>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth><Shell /></RequireAuth>}>
        <Route index element={<Home />} />
        <Route path="c/:clusterId" element={<Overview />} />
        <Route path="c/:clusterId/cluster-settings" element={<RequireAdmin><ClusterSettings /></RequireAdmin>} />
        <Route path="c/:clusterId/indices" element={<Indices />} />
        <Route path="c/:clusterId/data" element={<Data />} />
        <Route path="c/:clusterId/shell" element={<ShellPage />} />
        <Route path="c/:clusterId/indices/:index" element={<IndexDetail />} />
        <Route path="c/:clusterId/indices/:index/:part" element={<IndexDetail />} />
        {NAMED_TYPES.map((t) => (
          <Route key={t} path={`c/:clusterId/${t}`} element={<NamedResources key={t} type={t} />} />
        ))}
        {NAMED_TYPES.map((t) => (
          <Route key={`${t}-name`} path={`c/:clusterId/${t}/:name`} element={<NamedResources key={t} type={t} />} />
        ))}
        <Route path="account/password" element={<ChangePassword />} />
        <Route path="requests" element={<Requests />} />
        <Route path="requests/:requestId" element={<RequestDetail />} />
        <Route path="admin/clusters" element={<RequireAdmin><Clusters /></RequireAdmin>} />
        <Route path="admin/users" element={<RequireAdmin><Users /></RequireAdmin>} />
        <Route path="admin/allowlist" element={<RequireAdmin><Allowlist /></RequireAdmin>} />
        <Route path="admin/audit" element={<RequireAdmin><Audit /></RequireAdmin>} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
