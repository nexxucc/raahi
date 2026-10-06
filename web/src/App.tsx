import { useCallback, useEffect, useMemo, useState } from 'react'
import { Circle, MapContainer, Marker, TileLayer, useMap, useMapEvents } from 'react-leaflet'
import L from 'leaflet'
import {
  Activity, AlertTriangle, ArrowDownUp, ArrowRight, Bell, BusFront, Check,
  ChevronDown, CircleHelp, Clock3, Crosshair, LocateFixed, MapPin, Menu,
  Navigation, Plus, Radio, Search, ShieldCheck, Signal, Moon, Sun,
  Sparkles, TrainFront, X,
} from 'lucide-react'

type Route = { route_id: string; short_name?: string | null; long_name?: string | null; route_type: string; source: string }
type Stop = { stop_id: string; stop_name: string; latitude: number; longitude: number; distance_m?: number | null }
type Vehicle = {
  vehicle_id: string | null; route_id: string | null; direction_id?: string | null
  latitude: number; longitude: number; observed_at: string; received_at: string; age_seconds: number
  sources: string[]; confidence: number; status: 'live' | 'provisional' | 'stale' | 'simulated'
  crowd_contributors: number; disagreement_distance_m?: number | null; identity_kind: string
  route_match_status: string; freshness_status: 'fresh' | 'stale'
}
type Scenario = { id: string; label: string; description: string }
type Arrival = { stop_id: string; route_id: string; trip_id: string; scheduled_at: string; minutes_until: number; status: string; basis: string }
type Meta = { tracking_mode: 'live' | 'simulated'; official_feed_configured: boolean; limitations: string[] }
type ReportKind = 'position' | 'traffic' | 'delay' | 'breakdown' | 'crowding'

function vehicleKey(vehicle: Vehicle) {
  return vehicle.vehicle_id ?? `${vehicle.route_id ?? 'route'}:${vehicle.latitude}:${vehicle.longitude}:${vehicle.status}`
}

const center: [number, number] = [28.6139, 77.209]
const reportKinds: { value: ReportKind; label: string }[] = [
  { value: 'position', label: 'Bus location' }, { value: 'delay', label: 'Delay' },
  { value: 'traffic', label: 'Traffic' }, { value: 'breakdown', label: 'Breakdown' },
  { value: 'crowding', label: 'Crowding' },
]

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? `Request failed (${response.status})`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function vehicleIcon(status: Vehicle['status'], selected = false, unidentified = false) {
  const colors = { live: '#19896f', simulated: '#7764ca', provisional: '#d68b28', stale: '#9a9a92' }
  const color = colors[status]
  return L.divIcon({
    className: 'vehicle-marker-wrap',
    html: `<span class="vehicle-marker ${selected ? 'selected' : ''} ${unidentified ? 'unidentified' : ''}" style="--marker:${color}"><span><svg viewBox="0 0 24 24" aria-hidden="true">${unidentified ? '<path d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4m0-12.8L17 7M7 17l-1.4 1.4M9 12a3 3 0 1 0 6 0 3 3 0 0 0-6 0Z"/>' : '<path d="M6 16V5.5C6 3.6 8.7 3 12 3s6 .6 6 2.5V16M6 9h12M6 13h12M8 19l-1.5 2M16 19l1.5 2M7 16h10a2 2 0 0 1 2 2v.5H5V18a2 2 0 0 1 2-2Z"/>'}</svg></span></span>`,
    iconSize: [42, 42], iconAnchor: [21, 21],
  })
}

function StopPin({ stop, active, onClick }: { stop: Stop; active: boolean; onClick: () => void }) {
  const icon = useMemo(() => L.divIcon({
    className: 'stop-marker-wrap',
    html: `<span class="stop-marker ${active ? 'active' : ''}"><i></i></span>`,
    iconSize: [20, 20], iconAnchor: [10, 10],
  }), [active])
  return <Marker position={[stop.latitude, stop.longitude]} icon={icon} eventHandlers={{ click: onClick }} />
}

function MapClickCapture({ onPick }: { onPick: (position: [number, number]) => void }) {
  useMapEvents({ click(event) { onPick([event.latlng.lat, event.latlng.lng]) } })
  return null
}

function Recenter({ position }: { position: [number, number] }) {
  const map = useMap()
  useEffect(() => { map.flyTo(position, Math.max(map.getZoom(), 13), { duration: 0.7 }) }, [map, position])
  return null
}

function App() {
  const [routes, setRoutes] = useState<Route[]>([])
  const [stops, setStops] = useState<Stop[]>([])
  const [vehicles, setVehicles] = useState<Vehicle[]>([])
  const [arrivals, setArrivals] = useState<Arrival[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [selectedRoute, setSelectedRoute] = useState('')
  const [selectedStop, setSelectedStop] = useState<Stop | null>(null)
  const [selectedVehicle, setSelectedVehicle] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [queryResults, setQueryResults] = useState<{ routes: Route[]; stops: Stop[] } | null>(null)
  const [filter, setFilter] = useState<'all' | 'live' | 'community'>('all')
  const [reportOpen, setReportOpen] = useState(false)
  const [pickingReportLocation, setPickingReportLocation] = useState(false)
  const [reportPoint, setReportPoint] = useState<[number, number] | null>(null)
  const [reportKind, setReportKind] = useState<ReportKind>('position')
  const [consent, setConsent] = useState(false)
  const [reportBusy, setReportBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [apiError, setApiError] = useState('')
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [mobileMenu, setMobileMenu] = useState(false)
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('raahi-theme')
    return saved === 'dark' || saved === 'light' ? saved : (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  })
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [activeScenario, setActiveScenario] = useState('')
  const [scenarioBusy, setScenarioBusy] = useState(false)

  useEffect(() => { localStorage.setItem('raahi-theme', theme) }, [theme])

  const refreshNetwork = useCallback(async () => {
    try {
      const [routeData, stopData, metaData] = await Promise.all([
        api<Route[]>('/api/v1/routes?limit=100'), api<Stop[]>('/api/v1/stops?limit=100'), api<Meta>('/api/v1/meta'),
      ])
      setRoutes(routeData); setStops(stopData); setMeta(metaData); setApiError('')
      if (metaData.tracking_mode === 'simulated') {
        try {
          const demo = await api<{ active_scenario: string; scenarios: Scenario[] }>('/api/v1/demo/scenarios')
          setScenarios(demo.scenarios); setActiveScenario(demo.active_scenario)
        } catch { setScenarios([]); setActiveScenario('') }
      } else { setScenarios([]); setActiveScenario('') }
      setSelectedRoute(current => current || routeData[0]?.route_id || '')
      setSelectedStop(current => current || stopData[0] || null)
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'API unavailable')
    }
  }, [])

  const switchScenario = async (scenarioId: string) => {
    setScenarioBusy(true)
    try {
      const result = await api<{ active_scenario: string }>(`/api/v1/demo/scenarios/${encodeURIComponent(scenarioId)}`, { method: 'POST' })
      setActiveScenario(result.active_scenario); setSelectedVehicle(null)
      await refreshVehicles()
      setNotice(`Demo scenario changed to ${scenarios.find(item => item.id === scenarioId)?.label ?? scenarioId}.`)
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Could not change demo scenario') }
    finally { setScenarioBusy(false) }
  }

  useEffect(() => { void refreshNetwork() }, [refreshNetwork])

  const refreshVehicles = useCallback(async () => {
    if (!selectedRoute) { setVehicles([]); return }
    try {
      const items = await api<Vehicle[]>(`/api/v1/routes/${encodeURIComponent(selectedRoute)}/vehicles`)
      setVehicles(items); setLastUpdated(new Date()); setApiError('')
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Tracking request failed')
    }
  }, [selectedRoute])

  useEffect(() => {
    void refreshVehicles()
    const timer = window.setInterval(() => void refreshVehicles(), 15_000)
    return () => window.clearInterval(timer)
  }, [refreshVehicles])

  useEffect(() => {
    if (!selectedStop) { setArrivals([]); return }
    let cancelled = false
    api<Arrival[]>(`/api/v1/stops/${encodeURIComponent(selectedStop.stop_id)}/arrivals?limit=6`)
      .then(items => { if (!cancelled) setArrivals(items) })
      .catch(error => { if (!cancelled) setNotice(error instanceof Error ? error.message : 'Could not load arrivals') })
    return () => { cancelled = true }
  }, [selectedStop])

  useEffect(() => {
    const query = search.trim()
    if (query.length < 2) { setQueryResults(null); return }
    const timer = window.setTimeout(async () => {
      try {
        const [foundRoutes, foundStops] = await Promise.all([
          api<Route[]>(`/api/v1/routes?q=${encodeURIComponent(query)}&limit=8`),
          api<Stop[]>(`/api/v1/stops?q=${encodeURIComponent(query)}&limit=8`),
        ])
        setQueryResults({ routes: foundRoutes, stops: foundStops })
      } catch { setQueryResults({ routes: [], stops: [] }) }
    }, 250)
    return () => window.clearTimeout(timer)
  }, [search])

  const activeVehicle = vehicles.find(item => vehicleKey(item) === selectedVehicle) ?? vehicles[0] ?? null
  const selectedRouteInfo = routes.find(route => route.route_id === selectedRoute)
  const visibleVehicles = vehicles.filter(item => filter === 'all' || (filter === 'live' ? item.status === 'live' : item.sources.includes('crowd')))
  const mapPosition: [number, number] = activeVehicle ? [activeVehicle.latitude, activeVehicle.longitude] : selectedStop ? [selectedStop.latitude, selectedStop.longitude] : center
  const online = !apiError

  const chooseRoute = (route: Route) => {
    setSelectedRoute(route.route_id); setSearch(''); setQueryResults(null); setSelectedVehicle(null)
  }
  const chooseStop = (stop: Stop) => {
    setSelectedStop(stop); setSearch(''); setQueryResults(null); setReportPoint([stop.latitude, stop.longitude])
  }

  const useMyLocation = () => {
    if (!navigator.geolocation) { setNotice('Location is not available in this browser.'); return }
    navigator.geolocation.getCurrentPosition(
      position => { setReportPoint([position.coords.latitude, position.coords.longitude]); setNotice('Your current location is ready to attach to a report.') },
      () => setNotice('Location permission was not granted.'), { enableHighAccuracy: true, timeout: 8000 },
    )
  }

  const beginReport = () => { setReportPoint(selectedStop ? [selectedStop.latitude, selectedStop.longitude] : null); setReportOpen(true); setPickingReportLocation(false) }
  const pickOnMap = () => { setReportOpen(false); setPickingReportLocation(true); setNotice('Click the map to choose a report location.') }
  const navigateRail = (item: 'explore' | 'nearby' | 'routes' | 'reports') => {
    setMobileMenu(false)
    if (item === 'nearby') useMyLocation()
    if (item === 'routes') document.querySelector('.route-select')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    if (item === 'reports') beginReport()
  }

  const submitReport = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!consent) { setNotice('Please confirm the location-sharing consent to continue.'); return }
    if (!reportPoint) { setNotice('Choose a point on the map or use your current location first.'); return }
    setReportBusy(true)
    const tokenKey = 'raahi-ephemeral-contributor'
    let contributor = sessionStorage.getItem(tokenKey)
    if (!contributor) { contributor = crypto.randomUUID() + crypto.randomUUID(); sessionStorage.setItem(tokenKey, contributor) }
    try {
      await api('/api/v1/crowd/reports', { method: 'POST', body: JSON.stringify({
        consent: true, report_type: reportKind, latitude: reportPoint[0], longitude: reportPoint[1],
        observed_at: new Date().toISOString(), contributor_token: contributor,
        route_id: selectedRoute || null, vehicle_id: activeVehicle?.vehicle_id ?? null,
      }) })
      setReportOpen(false); setConsent(false); setNotice('Report shared. It will expire automatically within 30 minutes.')
      void refreshVehicles()
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Could not submit report') }
    finally { setReportBusy(false) }
  }

  return (
    <div className={`app-shell ${theme === 'dark' ? 'dark-theme' : ''}`}>
      <header className="topbar">
        <div className="brand-lockup">
          <button className="mobile-menu icon-button" aria-label="Open menu" onClick={() => setMobileMenu(!mobileMenu)}><Menu size={19} /></button>
          <div className="brand-mark"><Navigation size={19} fill="currentColor" /></div>
          <div><div className="brand-name">raahi<span>.</span></div><div className="brand-caption">DELHI TRANSIT</div></div>
        </div>
        <div className="top-search-wrap">
          <Search size={17} />
          <input aria-label="Search routes and stops" placeholder="Search buses, routes or stops" value={search} onChange={event => setSearch(event.target.value)} />
          <kbd>⌘ K</kbd>
          {queryResults && <div className="search-results">
            {queryResults.routes.length === 0 && queryResults.stops.length === 0 && <div className="search-empty">No matches found</div>}
            {queryResults.routes.map(route => <button key={route.route_id} onClick={() => chooseRoute(route)}><span className="search-result-icon route-icon"><BusFront size={16} /></span><span><b>Route {route.short_name || route.route_id}</b><small>{route.long_name || route.route_id}</small></span><ArrowRight size={15} /></button>)}
            {queryResults.stops.map(stop => <button key={stop.stop_id} onClick={() => chooseStop(stop)}><span className="search-result-icon"><MapPin size={16} /></span><span><b>{stop.stop_name}</b><small>Bus stop · {stop.stop_id}</small></span><ArrowRight size={15} /></button>)}
          </div>}
        </div>
        <div className="header-actions">
          <div className={`api-indicator ${online ? 'connected' : 'disconnected'}`}><i />{online ? 'API connected' : 'API offline'}</div>
          <button className="icon-button theme-toggle" aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button>
          <button className="icon-button bell-button" aria-label="Notifications" onClick={() => setNotice('Arrival notifications are a future feature. Scheduled arrivals are available below.') }><Bell size={18} /><i /></button>
          <div className="avatar">R</div>
        </div>
      </header>

      <div className="workspace">
        <aside className={`left-rail ${mobileMenu ? 'open' : ''}`}>
          <div className="rail-group">
            <button className="rail-item active" title="Explore" onClick={() => navigateRail('explore')}><MapPin size={19} /><span>Explore</span></button>
            <button className="rail-item" title="Nearby" onClick={() => navigateRail('nearby')}><Crosshair size={19} /><span>Nearby</span></button>
            <button className="rail-item" title="Routes" onClick={() => navigateRail('routes')}><ArrowDownUp size={19} /><span>Routes</span></button>
            <button className="rail-item" title="Report" onClick={() => navigateRail('reports')}><Bell size={19} /><span>Report</span></button>
          </div>
          <div className="rail-bottom"><span className="mini-status-dot" /><small>DELHI NCR</small><button className="help-button" title="About this demo" onClick={() => setNotice('Raahi is a Delhi transit tracking prototype. Simulated, crowd reported and schedule based data are labelled separately.')}><CircleHelp size={17} /></button></div>
        </aside>

        <main className="main-content">
          <div className="page-heading">
            <div><div className="eyebrow"><span className="green-dot" /> YOUR CITY, IN MOTION</div><h1>Good morning, commuter <span>✳</span></h1><p>Find your bus. Know when it’s coming. Get there with confidence.</p></div>
            <button className="report-button" onClick={beginReport}><Plus size={17} /> Share a report</button>
          </div>

          <div className="stats-row">
            <div className="stat-card"><div className="stat-icon mint"><BusFront size={17} /></div><div><span>POSITIONS ON ROUTE</span><strong>{vehicles.length.toString().padStart(2, '0')} <small>{selectedRouteInfo?.short_name || '—'}</small></strong></div></div>
            <div className="stat-card"><div className="stat-icon lilac"><Radio size={17} /></div><div><span>TRACKING MODE</span><strong className="status-reading"><i className={meta?.tracking_mode === 'live' ? 'green-dot' : 'purple-dot'} />{meta?.tracking_mode === 'live' ? 'Live' : 'Simulated'}</strong></div></div>
            <div className="stat-card"><div className="stat-icon peach"><MapPin size={17} /></div><div><span>NEARBY STOPS</span><strong>{stops.length.toString().padStart(2, '0')} <small>in network</small></strong></div></div>
            <div className="stat-card update-stat"><div className="stat-icon gold"><Clock3 size={17} /></div><div><span>LAST REFRESH</span><strong>{lastUpdated ? `${Math.max(0, Math.round((Date.now() - lastUpdated.getTime()) / 1000))}s ago` : 'Waiting'}</strong></div></div>
          </div>

          <div className="content-grid">
            <section className="map-panel panel">
              <div className="panel-head map-head">
                <div><div className="panel-title">Live map <span className={meta?.tracking_mode === 'live' ? 'live-pill' : 'demo-pill'}><i />{meta?.tracking_mode === 'live' ? 'LIVE' : 'DEMO MODE'}</span></div><div className="panel-subtitle">{selectedRouteInfo?.long_name || 'Explore buses and stops around you'}</div></div>
                <div className="map-actions"><button className="soft-icon" title="My location" onClick={useMyLocation}><LocateFixed size={16} /></button></div>
              </div>
              <div className="map-frame">
                <MapContainer center={center} zoom={13} scrollWheelZoom className="leaflet-map">
                  <TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
                  {selectedStop && <Recenter position={mapPosition} />}
                  <MapClickCapture onPick={point => { if (pickingReportLocation) { setReportPoint(point); setPickingReportLocation(false); setReportOpen(true); setNotice('Location selected. Review your report before sharing.') } }} />
                  {stops.map(stop => <StopPin key={stop.stop_id} stop={stop} active={selectedStop?.stop_id === stop.stop_id} onClick={() => chooseStop(stop)} />)}
                  {visibleVehicles.map(vehicle => <Marker key={vehicleKey(vehicle)} position={[vehicle.latitude, vehicle.longitude]} icon={vehicleIcon(vehicle.status, selectedVehicle === vehicleKey(vehicle), vehicle.identity_kind === 'unidentified_crowd_cluster')} eventHandlers={{ click: () => setSelectedVehicle(vehicleKey(vehicle)) }} />)}
                  {reportOpen && reportPoint && <Circle center={reportPoint} radius={45} pathOptions={{ color: '#d27b46', fillColor: '#f1a16b', fillOpacity: 0.28, weight: 2 }} />}
                </MapContainer>
                <div className="map-floating-label"><span className="map-location-dot" /> CENTRAL DELHI <ChevronDown size={13} /></div>
                <div className="map-legend"><span><i className="legend-bus live" />Bus</span><span><i className="legend-stop" />Stop</span><span><i className="legend-bus provisional" />Community</span></div>
                {pickingReportLocation && <div className="map-instruction"><MapPin size={15} /> Click the map to place your report</div>}
                {stops.length === 0 && <div className="map-empty"><div className="empty-map-icon"><TrainFront size={20} /></div><b>Your map is ready</b><span>Import a GTFS feed or load the synthetic demo network to see routes, stops and buses.</span><code>python -m app.replay --database data/transit.sqlite3</code></div>}
              </div>
              <div className="map-footer"><span><ShieldCheck size={14} /> Sources and confidence are shown for every position</span><button onClick={() => setNotice('Positions may be simulated, crowd reported, stale, or schedule based. Check each label before relying on it.')}>How tracking works <ArrowRight size={14} /></button></div>
            </section>

            <aside className="right-column">
              <section className="panel vehicle-panel">
                <div className="panel-head"><div><div className="panel-title">On the move <span className="count-pill">{vehicles.length}</span></div><div className="panel-subtitle">{meta?.tracking_mode === 'simulated' ? 'Clearly labelled demo positions' : 'Buses reporting on this route'}</div></div><button className="soft-icon" onClick={() => void refreshVehicles()} title="Refresh"><Activity size={16} /></button></div>
                {scenarios.length > 0 && <div className="scenario-control"><label htmlFor="scenario-select">DEMO SCENARIO</label><select id="scenario-select" aria-label="Demo scenario" value={activeScenario} disabled={scenarioBusy} onChange={event => void switchScenario(event.target.value)}>{scenarios.map(scenario => <option key={scenario.id} value={scenario.id}>{scenario.label}</option>)}</select><small>{scenarios.find(item => item.id === activeScenario)?.description}</small></div>}
                <div className="route-select"><span className="route-color" /><select aria-label="Select route" value={selectedRoute} onChange={event => { setSelectedRoute(event.target.value); setSelectedVehicle(null) }}><option value="">Select a route</option>{routes.map(route => <option key={route.route_id} value={route.route_id}>Route {route.short_name || route.route_id} · {route.long_name || ''}</option>)}</select><ChevronDown size={15} /></div>
                <div className="filter-tabs"><button className={filter === 'all' ? 'selected' : ''} onClick={() => setFilter('all')}>All <span>{vehicles.length}</span></button><button className={filter === 'live' ? 'selected' : ''} onClick={() => setFilter('live')}>Live</button><button className={filter === 'community' ? 'selected' : ''} onClick={() => setFilter('community')}>Community</button></div>
                <div className="vehicle-list">
                  {visibleVehicles.map((vehicle) => <button key={vehicleKey(vehicle)} className={`vehicle-row ${activeVehicle === vehicle ? 'chosen' : ''}`} onClick={() => setSelectedVehicle(vehicleKey(vehicle))}>
                    <span className={`bus-avatar ${vehicle.status}`}><BusFront size={17} /></span>
                    <span className="vehicle-main"><b>{vehicle.vehicle_id || 'Unidentified bus'}</b><small>{vehicle.route_id || selectedRoute} <span>·</span> {vehicle.direction_id ? `Direction ${vehicle.direction_id}` : vehicle.identity_kind === 'unidentified_crowd_cluster' ? 'Community sighting' : 'Direction unknown'}</small><span className="vehicle-source">{vehicle.sources.join(' + ')} <i>·</i> {vehicle.crowd_contributors} rider{vehicle.crowd_contributors === 1 ? '' : 's'}</span></span>
                    <span className="vehicle-age"><b>{vehicle.age_seconds < 60 ? `${vehicle.age_seconds}s` : `${Math.floor(vehicle.age_seconds / 60)}m`}</b><small>{vehicle.status}{vehicle.freshness_status === 'stale' ? ' · stale' : ''}</small></span>
                  </button>)}
                  {visibleVehicles.length === 0 && <div className="empty-list"><div className="empty-icon"><BusFront size={18} /></div><b>{selectedRoute ? 'No buses reporting yet' : 'Choose a route'}</b><span>{selectedRoute ? 'Reports appear here when fresh official positions or independent rider reports are available.' : 'Pick a route above to see its current tracking state.'}</span></div>}
                </div>
                {activeVehicle && <div className="confidence-card"><div className="confidence-top"><span>POSITION CONFIDENCE</span><strong>{Math.round(activeVehicle.confidence * 100)}%</strong></div><div className="confidence-track"><i style={{ width: `${Math.round(activeVehicle.confidence * 100)}%` }} /></div><div className="confidence-meta"><span><Signal size={13} /> {activeVehicle.sources.join(' + ')}</span><span>{activeVehicle.route_match_status.replaceAll('_', ' ')}</span></div>{activeVehicle.disagreement_distance_m != null && <div className="disagreement"><AlertTriangle size={13} /> Rider reports differ by {Math.round(activeVehicle.disagreement_distance_m)}m</div>}</div>}
                <button className="text-link full-link" onClick={() => setFilter(filter === 'all' ? 'live' : 'all')}>View {filter === 'all' ? 'live only' : 'all reports'} <ArrowRight size={14} /></button>
              </section>

              <section className="panel arrivals-panel">
                <div className="panel-head"><div><div className="panel-title">Next arrivals <span className="schedule-label">SCHEDULE</span></div><div className="panel-subtitle">{selectedStop?.stop_name || 'Select a stop on the map'}</div></div><button className="soft-icon"><ArrowDownUp size={16} /></button></div>
                {stops.length > 0 && <div className="stop-select-wrap"><MapPin size={14} /><select aria-label="Select stop" value={selectedStop?.stop_id ?? ''} onChange={event => setSelectedStop(stops.find(stop => stop.stop_id === event.target.value) ?? null)}>{stops.map(stop => <option value={stop.stop_id} key={stop.stop_id}>{stop.stop_name}</option>)}</select><ChevronDown size={14} /></div>}
                <div className="arrival-list">
                  {arrivals.slice(0, 3).map(arrival => <div className="arrival-row" key={arrival.trip_id}><div className="arrival-route"><span className="route-badge">{routes.find(route => route.route_id === arrival.route_id)?.short_name || arrival.route_id}</span><span><b>Bus service</b><small>Scheduled · {new Date(arrival.scheduled_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</small></span></div><strong>{arrival.minutes_until}<small> min</small></strong></div>)}
                  {arrivals.length === 0 && <div className="no-arrivals"><Clock3 size={16} /><span>No upcoming scheduled arrivals in the next 24 hours.</span></div>}
                </div>
                <div className="schedule-foot"><Clock3 size={13} /> Timetable estimates · may vary from actual service</div>
              </section>
            </aside>
          </div>

          <section className="bottom-strip">
            <div className="service-note"><div className="note-icon"><Sparkles size={17} /></div><div><b>Every report makes the picture clearer</b><span>Share a sighting or service update to help other riders travel smarter.</span></div><button onClick={() => { setReportOpen(true); setReportPoint(null) }}>Share an update <ArrowRight size={14} /></button></div>
            <div className="footnote"><span>Built for Delhi commuters</span><span><span className="green-dot" /> API {online ? 'connected' : 'disconnected'}</span><a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API documentation <ArrowRight size={12} /></a></div>
          </section>
        </main>
      </div>

      {apiError && <div className="toast error-toast"><AlertTriangle size={16} /><span><b>Couldn’t reach the tracker API</b><small>{apiError} · Is the FastAPI server running?</small></span><button onClick={() => void refreshNetwork()}><Activity size={15} /> Retry</button></div>}
      {notice && <div className="toast notice-toast"><Check size={16} /><span>{notice}</span><button aria-label="Dismiss" onClick={() => setNotice('')}><X size={15} /></button></div>}

      {reportOpen && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setReportOpen(false) }}>
        <form className="report-modal" onSubmit={submitReport}>
          <div className="modal-heading"><div><span className="eyebrow">COMMUNITY SIGNAL</span><h2>Share a report</h2><p>Help another commuter know what’s happening on the ground.</p></div><button type="button" className="icon-button" onClick={() => setReportOpen(false)} aria-label="Close"><X size={19} /></button></div>
          <label className="field-label">What would you like to report?</label>
          <div className="report-kind-grid">{reportKinds.map(kind => <button type="button" key={kind.value} className={reportKind === kind.value ? 'kind-selected' : ''} onClick={() => setReportKind(kind.value)}>{kind.value === 'position' ? <BusFront size={16} /> : kind.value === 'traffic' ? <ArrowDownUp size={16} /> : kind.value === 'delay' ? <Clock3 size={16} /> : kind.value === 'breakdown' ? <AlertTriangle size={16} /> : <Signal size={16} />}{kind.label}</button>)}</div>
          <div className="location-picker"><div><MapPin size={16} /><span><b>{reportPoint ? `${reportPoint[0].toFixed(5)}, ${reportPoint[1].toFixed(5)}` : 'Choose a location'}</b><small>{selectedStop ? `Selected stop: ${selectedStop.stop_name}` : 'Click the map or share your device location'}</small></span></div><div className="location-actions"><button type="button" onClick={pickOnMap}><Crosshair size={13} /> Pick on map</button><button type="button" onClick={useMyLocation}><LocateFixed size={14} /></button></div></div>
          <div className="map-pick-hint"><Crosshair size={14} /> You can also click directly on the map to place the report pin.</div>
          <label className="consent-row"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} /><span>I agree to share this location for this report. It will be stored temporarily (up to 30 minutes) and won’t include my name.</span></label>
          <div className="modal-actions"><button type="button" className="cancel-button" onClick={() => setReportOpen(false)}>Cancel</button><button type="submit" className="submit-report" disabled={reportBusy}>{reportBusy ? 'Sending…' : 'Share report'}<ArrowRight size={15} /></button></div>
          <div className="privacy-hint"><ShieldCheck size={13} /> Your session uses an ephemeral contributor token. No account required.</div>
        </form>
      </div>}
    </div>
  )
}

export default App
