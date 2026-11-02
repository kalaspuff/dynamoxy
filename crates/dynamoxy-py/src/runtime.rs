//! The Tokio runtime behind every awaitable returned by `dynamoxy._core`, and the bridge that
//! turns Rust futures into asyncio futures.
//!
//! The runtime is created lazily and is fork-aware: a process forked from a parent that
//! already started the runtime gets a fresh runtime (the parent's threads do not exist in
//! the child; the inherited runtime is leaked instead of dropped, because dropping it
//! would wait for threads that are gone). Clients created before a fork refuse to work in
//! the child (see `PyCoreClient::client`).
//!
//! The runtime is shut down by an `atexit` hook ([`shutdown`]), before the interpreter
//! starts finalizing; afterwards every awaitable raises `ShutdownError`.
//!
//! # The asyncio bridge
//!
//! Runtime threads never attach to Python. A call returns an `asyncio.Future` of the running
//! event loop, and its Rust future runs on the runtime. When it finishes, its outcome (a
//! [`Deferred`] that builds the Python result) is queued on the loop's [`Port`]; the first
//! completion after a drain wakes the loop by writing one byte to a socket the loop watches
//! (`loop.add_reader`). The loop's thread then builds the Python results of the queued calls
//! and resolves their futures: no thread waits for the GIL, and conversions run where the GIL
//! is held anyway. A drain builds results for about [`STEP_BUDGET`] and then schedules itself
//! again (`loop.call_soon`), and large results (pages of items) are built in steps of that
//! length, so that other tasks run in between. Loops without `add_reader` (the Windows proactor loop) are woken with
//! `call_soon_threadsafe` from a blocking thread instead.
//!
//! In-memory clients poll their future once on the calling thread first ([`future_into_py_inline`]):
//! operations without injected latency finish right there, with no thread hop. Their awaitable
//! ([`Ready`]) still yields to the event loop once, like real I/O, before it returns the result
//! (and once after each step of building a large one).
//!
//! Cancelling the asyncio future aborts the Rust task: it is dropped at its next await point.

use std::any::Any;
use std::cell::RefCell;
use std::collections::{HashMap, VecDeque};
use std::future::Future;
#[cfg(unix)]
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, OnceLock, Weak};
use std::task::{Context, Poll, Waker};
use std::time::{Duration, Instant};

use parking_lot::Mutex;
#[cfg(unix)]
use pyo3::exceptions::PyNotImplementedError;
use pyo3::exceptions::{PyRuntimeError, PyStopIteration};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyWeakrefReference;
use pyo3::{IntoPyObjectExt, intern};
use tokio::runtime::{Builder, Handle, Runtime};
use tokio::task::AbortHandle;

use crate::errors::{DynamoxyError, ShutdownError};

/// The runtime and the process that created it.
static RUNTIME: Mutex<Option<(u32, Runtime)>> = Mutex::new(None);
/// After [`shutdown`]: the handle of the stopped runtime (spawning on it cancels the task).
static STOPPED: Mutex<Option<Handle>> = Mutex::new(None);
static SHUT_DOWN: AtomicBool = AtomicBool::new(false);
/// How long [`shutdown`] waits for running tasks (they stop at their next await point).
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

fn worker_threads() -> usize {
    std::env::var("DYNAMOXY_WORKER_THREADS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|threads| *threads > 0)
        .unwrap_or_else(|| std::thread::available_parallelism().map_or(2, |n| n.get().clamp(2, 4)))
}

/// A handle to the current process's runtime (created on first use).
///
/// After [`shutdown`] this is the handle of the stopped runtime: tasks spawned on it are
/// cancelled immediately instead of starting new threads.
pub fn handle() -> Handle {
    handle_for(std::process::id())
}

/// [`handle`] for a caller that knows the current process id (saving a system call).
fn handle_for(pid: u32) -> Handle {
    let mut guard = RUNTIME.lock();
    if let Some((owner, runtime)) = guard.as_ref()
        && *owner == pid
    {
        return runtime.handle().clone();
    }
    if let Some((_, inherited)) = guard.take() {
        // Inherited from the parent across fork(): its worker threads do not exist here.
        std::mem::forget(inherited);
    }
    if SHUT_DOWN.load(Ordering::Acquire)
        && let Some(stopped) = STOPPED.lock().clone()
    {
        return stopped;
    }
    let runtime = Builder::new_multi_thread()
        .enable_all()
        .worker_threads(worker_threads())
        .thread_name("dynamoxy-worker")
        .build()
        .expect("failed to start the dynamoxy Tokio runtime");
    let handle = runtime.handle().clone();
    *guard = Some((pid, runtime));
    handle
}

/// Whether [`shutdown`] has run (the interpreter is exiting).
pub fn is_shut_down() -> bool {
    SHUT_DOWN.load(Ordering::Acquire)
}

/// Stops the runtime: running tasks are dropped at their next await point and queued work
/// is discarded. Waits (without holding the GIL) up to [`SHUTDOWN_TIMEOUT`]. Registered with
/// `atexit` on import.
pub fn shutdown(py: Python<'_>) {
    SHUT_DOWN.store(true, Ordering::Release);
    let taken = RUNTIME.lock().take();
    let Some((owner, runtime)) = taken else {
        return;
    };
    if owner != std::process::id() {
        std::mem::forget(runtime);
        return;
    }
    *STOPPED.lock() = Some(runtime.handle().clone());
    py.detach(|| runtime.shutdown_timeout(SHUTDOWN_TIMEOUT));
}

/// Drops `value` on a thread of the runtime's blocking pool, or right here once it is shut down:
/// freeing large Rust values (the items of a page converted to Python) takes milliseconds that
/// the event loop's thread need not spend.
pub fn drop_elsewhere<T: Send + 'static>(value: T) {
    if is_shut_down() {
        drop(value);
        return;
    }
    // A runtime shut down meanwhile drops the closure, and the value with it, right here.
    drop(handle().spawn_blocking(move || drop(value)));
}

/// Forgets the runtime inherited from a parent process (called after `fork()`).
pub fn reset_after_fork() {
    let pid = std::process::id();
    let mut guard = RUNTIME.lock();
    if guard.as_ref().is_some_and(|(owner, _)| *owner != pid)
        && let Some((_, inherited)) = guard.take()
    {
        std::mem::forget(inherited);
    }
}

// ---- results --------------------------------------------------------------------------------

/// One step of building an outcome: `None` while there is more to do (it stops soon after the
/// deadline it is given), then the result.
type Step = Box<dyn for<'py> FnMut(Python<'py>, Instant) -> Option<PyResult<Py<PyAny>>> + Send>;

/// How long the event loop's thread builds outcomes before it lets other tasks run: a large
/// result (a page of thousands of items) is converted in steps of about this long.
pub const STEP_BUDGET: Duration = Duration::from_millis(1);

/// The outcome of a call, turned into Python objects (a result or an exception) only on the
/// event loop's thread: at once, or in steps of about [`STEP_BUDGET`] (results with many items),
/// between which the loop runs other tasks.
pub struct Deferred(Step);

impl Deferred {
    /// An outcome built by `build`, which runs on the event loop's thread with the GIL.
    pub fn new(build: impl for<'py> FnOnce(Python<'py>) -> PyResult<Py<PyAny>> + Send + 'static) -> Self {
        let mut build = Some(build);
        Self(Box::new(move |py: Python<'_>, _deadline: Instant| {
            Some(match build.take() {
                Some(build) => build(py),
                None => Err(PyRuntimeError::new_err("internal error: an outcome was built twice")),
            })
        }))
    }

    /// An outcome built in steps by `step`, which runs on the event loop's thread with the GIL
    /// until it returns the result; each call should return soon after its deadline.
    pub fn in_steps(
        step: impl for<'py> FnMut(Python<'py>, Instant) -> Option<PyResult<Py<PyAny>>> + Send + 'static,
    ) -> Self {
        Self(Box::new(step))
    }

    /// A plain Rust value, converted with `IntoPyObject`.
    pub fn value<T>(value: T) -> Self
    where
        T: for<'py> IntoPyObject<'py> + Send + 'static,
    {
        Self::new(move |py| value.into_py_any(py))
    }
}

/// A finished call: its outcome, or the message of the panic that ended it.
type Finished = Result<Deferred, String>;

fn panic_message(payload: Box<dyn Any + Send>) -> String {
    if let Some(text) = payload.downcast_ref::<&str>() {
        (*text).to_owned()
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text.clone()
    } else {
        "unknown panic".to_owned()
    }
}

/// Turns a panic of the wrapped future into an error result, like a failed call.
struct CatchUnwind<F: Future>(Pin<Box<F>>);

impl<F: Future> Future for CatchUnwind<F> {
    type Output = Result<F::Output, String>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let inner = self.get_mut().0.as_mut();
        match catch_unwind(AssertUnwindSafe(|| inner.poll(cx))) {
            Ok(Poll::Pending) => Poll::Pending,
            Ok(Poll::Ready(output)) => Poll::Ready(Ok(output)),
            Err(payload) => Poll::Ready(Err(panic_message(payload))),
        }
    }
}

// ---- ports ----------------------------------------------------------------------------------

/// How the runtime's threads wake an event loop.
enum Wake {
    /// Write one byte to a socket pair whose other end the loop watches with `add_reader`. Both
    /// ends live as long as a task may still write (no write ever meets a closed peer).
    #[cfg(unix)]
    Socket { reader: UnixStream, writer: UnixStream },
    /// `loop.call_soon_threadsafe(port.drain)` from a blocking thread (needs the GIL). Weak
    /// references, so that pending calls keep neither the loop nor the port alive.
    Threadsafe { event_loop: Py<PyWeakrefReference>, port: Py<PyWeakrefReference> },
}

/// What the runtime's threads share with one event loop: finished calls and how to wake it.
struct Shared {
    finished: Mutex<Vec<(u64, Finished)>>,
    wake: OnceLock<Wake>,
}

impl Shared {
    /// Queues a finished call; the first one after a drain wakes the loop.
    fn finish(self: &Arc<Self>, id: u64, finished: Finished) {
        let first = {
            let mut queue = self.finished.lock();
            queue.push((id, finished));
            queue.len() == 1
        };
        if first {
            self.wake();
        }
    }

    fn wake(self: &Arc<Self>) {
        match self.wake.get() {
            #[cfg(unix)]
            Some(Wake::Socket { writer, .. }) => {
                if !write_wakeup(&mut &*writer) {
                    // No byte is pending (ENOBUFS, ENOMEM, ...): the calls finishing later would
                    // not write one either, so write it again until it is.
                    let shared = Arc::clone(self);
                    handle().spawn(async move {
                        let mut delay = std::time::Duration::from_millis(1);
                        loop {
                            tokio::time::sleep(delay).await;
                            let Some(Wake::Socket { writer, .. }) = shared.wake.get() else { return };
                            if is_shut_down() || write_wakeup(&mut &*writer) {
                                return;
                            }
                            delay = (delay * 2).min(std::time::Duration::from_millis(100));
                        }
                    });
                }
            }
            Some(Wake::Threadsafe { .. }) => {
                let shared = Arc::clone(self);
                handle().spawn_blocking(move || {
                    // Nothing may touch Python once the interpreter is exiting.
                    if is_shut_down() {
                        return;
                    }
                    Python::attach(|py| {
                        let Some(Wake::Threadsafe { event_loop, port }) = shared.wake.get() else { return };
                        let (Some(event_loop), Some(port)) = (event_loop.bind(py).upgrade(), port.bind(py).upgrade())
                        else {
                            return;
                        };
                        if let Ok(drain) = port.getattr(intern!(py, "drain")) {
                            // Fails when the loop is closed: nobody waits for these calls any more.
                            let _ = event_loop.call_method1(intern!(py, "call_soon_threadsafe"), (drain,));
                        }
                    });
                });
            }
            None => {}
        }
    }
}

/// The asyncio futures of calls in progress on one event loop (used on the loop's thread only).
struct Calls {
    futures: Mutex<HashMap<u64, Pending>>,
    /// Finished calls whose outcome a drain did not build within its budget, oldest first, and
    /// whether a drain is scheduled (`loop.call_soon`) to go on with them.
    converting: Mutex<(VecDeque<(u64, Finished)>, bool)>,
}

/// A call's asyncio future and its done callback.
type Pending = (Py<PyAny>, Py<Canceller>);

/// The bridge's end on one event loop: resolves the futures of finished calls.
#[pyclass(frozen, weakref, module = "dynamoxy._core")]
pub struct Port {
    shared: Arc<Shared>,
    calls: Arc<Calls>,
    next_id: AtomicU64,
    event_loop: Py<PyWeakrefReference>,
    pid: u32,
}

#[pymethods]
impl Port {
    /// Resolves the futures of finished calls (called by the event loop), building their
    /// outcomes for about [`STEP_BUDGET`]: when calls are left, it schedules itself again
    /// (`loop.call_soon`), so that other tasks run in between.
    fn drain(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let port = slf.get();
        // Read the wake-up bytes before taking the queue: a byte read here was written after a
        // call was queued, so that call is taken below. Bytes written later wake the loop again.
        #[cfg(unix)]
        if let Some(Wake::Socket { reader, .. }) = port.shared.wake.get() {
            let mut buffer = [0u8; 256];
            // A short read took every byte; bytes left over only cause another (empty) drain.
            while matches!((&*reader).read(&mut buffer), Ok(count) if count == buffer.len()) {}
        }
        let mut queue = {
            let mut converting = port.calls.converting.lock();
            converting.1 = false;
            std::mem::take(&mut converting.0)
        };
        queue.extend(std::mem::take(&mut *port.shared.finished.lock()));
        let deadline = Instant::now() + STEP_BUDGET;
        let mut built = false;
        while let Some((id, mut finished)) = queue.pop_front() {
            if built && Instant::now() >= deadline {
                queue.push_front((id, finished));
                break;
            }
            let future = port.calls.futures.lock().get(&id).map(|(future, _)| future.clone_ref(py));
            // Gone: cancelled (its canceller removed it), or its loop closed.
            let Some(future) = future else { continue };
            let future = future.bind(py);
            if !matches!(future.call_method0(intern!(py, "cancelled")).and_then(|value| value.is_truthy()), Ok(false)) {
                let entry = port.calls.futures.lock().remove(&id);
                drop(entry);
                continue;
            }
            built = true;
            let Some(outcome) = step(py, &mut finished, deadline) else {
                queue.push_front((id, finished));
                break;
            };
            let entry = port.calls.futures.lock().remove(&id);
            if let Some((_, canceller)) = entry {
                let _ = future.call_method1(intern!(py, "remove_done_callback"), (canceller,));
            }
            let _ = match outcome {
                Ok(value) => future.call_method1(intern!(py, "set_result"), (value,)),
                Err(error) => future.call_method1(intern!(py, "set_exception"), (error.into_value(py),)),
            };
        }
        if queue.is_empty() {
            return Ok(());
        }
        let schedule = {
            let mut converting = port.calls.converting.lock();
            // Older than anything a nested drain may have left (none: this is the loop's thread).
            queue.extend(std::mem::take(&mut converting.0));
            converting.0 = queue;
            !std::mem::replace(&mut converting.1, true)
        };
        if schedule && let Some(event_loop) = port.event_loop.bind(py).upgrade() {
            event_loop.call_method1(intern!(py, "call_soon"), (slf.getattr(intern!(py, "drain"))?,))?;
        }
        Ok(())
    }
}

/// Builds the outcome of a finished call for a while: `None` when it is not built yet by
/// `deadline`, else the Python result (or exception).
fn step(py: Python<'_>, finished: &mut Finished, deadline: Instant) -> Option<PyResult<Py<PyAny>>> {
    match finished {
        Ok(deferred) => (deferred.0)(py, deadline),
        Err(message) => {
            Some(Err(DynamoxyError::new_err(format!("internal error: a dynamoxy task panicked: {message}"))))
        }
    }
}

/// Writes the wake-up byte. Returns whether a byte is pending for the loop: written, or the
/// buffer is full (`WouldBlock`), which means one is pending anyway. Interrupted writes are
/// retried; other errors leave nothing pending.
#[cfg(unix)]
fn write_wakeup(writer: &mut impl Write) -> bool {
    loop {
        match writer.write(&[1]) {
            Ok(written) => return written == 1,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
            Err(error) => return error.kind() == std::io::ErrorKind::WouldBlock,
        }
    }
}

/// The awaitable of a call that finished when it was made (in-memory backend): awaiting it
/// yields to the event loop once (a bare yield, like `asyncio.sleep(0)`), then returns the
/// result, which is converted to Python only then, in steps of about [`STEP_BUDGET`] with a
/// yield after each one.
#[pyclass(frozen, module = "dynamoxy._core")]
pub struct Ready {
    /// Whether the awaitable yielded already, and the outcome until it is returned.
    state: Mutex<(bool, Option<Finished>)>,
}

#[pymethods]
impl Ready {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// The awaitable is its own iterator, also for `yield from call.__await__()` in generator
    /// coroutines, as for an `asyncio.Future`.
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let finished = {
            let mut state = self.state.lock();
            if !state.0 {
                state.0 = true;
                return Ok(py.None());
            }
            state.1.take()
        };
        let Some(mut finished) = finished else {
            return Err(PyRuntimeError::new_err("cannot reuse an awaited dynamoxy call"));
        };
        // Not under the lock: building Python objects may run code (a collection's finalizers).
        match step(py, &mut finished, Instant::now() + STEP_BUDGET) {
            Some(outcome) => Err(PyStopIteration::new_err((outcome?,))),
            None => {
                self.state.lock().1 = Some(finished);
                Ok(py.None())
            }
        }
    }
}

/// Done callback of a call's future: aborts the Rust task when the future is cancelled.
#[pyclass(frozen, module = "dynamoxy._core")]
pub struct Canceller {
    id: u64,
    task: AbortHandle,
    calls: Weak<Calls>,
}

#[pymethods]
impl Canceller {
    fn __call__(&self, future: &Bound<'_, PyAny>) -> PyResult<()> {
        if future.call_method0(intern!(future.py(), "cancelled"))?.is_truthy()? {
            self.task.abort();
            if let Some(calls) = self.calls.upgrade() {
                let entry = calls.futures.lock().remove(&self.id);
                drop(entry);
            }
        }
        Ok(())
    }
}

thread_local! {
    /// The port of the event loop that last made a call on this thread.
    static CURRENT: RefCell<Option<Py<Port>>> = const { RefCell::new(None) };
}

static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
/// Ports by event loop (a `weakref.WeakKeyDictionary`), for threads switching between loops.
static PORTS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn ports(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    PORTS
        .get_or_try_init(py, || Ok::<_, PyErr>(py.import("weakref")?.getattr("WeakKeyDictionary")?.call0()?.unbind()))
        .map(|registry| registry.bind(py))
}

fn serves<'py>(port: &Bound<'py, Port>, event_loop: &Bound<'py, PyAny>, pid: u32) -> bool {
    let port = port.get();
    port.pid == pid && port.event_loop.bind(event_loop.py()).upgrade().is_some_and(|l| l.is(event_loop))
}

/// The port of `event_loop` (in the process `pid`), created on first use.
fn port_for<'py>(event_loop: &Bound<'py, PyAny>, pid: u32) -> PyResult<Bound<'py, Port>> {
    let py = event_loop.py();
    let cached = CURRENT.with_borrow(|current| current.as_ref().map(|port| port.bind(py).clone()));
    if let Some(port) = cached
        && serves(&port, event_loop, pid)
    {
        return Ok(port);
    }
    let registry = ports(py)?;
    let port = match registry.call_method1(intern!(py, "get"), (event_loop,))?.cast_into::<Port>() {
        Ok(port) if serves(&port, event_loop, pid) => port,
        _ => {
            release_closed(registry)?;
            let port = create_port(event_loop, pid)?;
            registry.set_item(event_loop, &port)?;
            port
        }
    };
    // The previous port is dropped after the borrow (dropping Python objects may run code).
    let previous = CURRENT.with_borrow_mut(|current| current.replace(port.clone().unbind()));
    drop(previous);
    Ok(port)
}

/// Drops the pending calls of ports whose event loop is closed: nobody can await them any more,
/// and their futures would keep the loop (and, through the registry, the port) alive for good.
/// asyncio drops the callbacks of a closed loop too. Their tasks finish on their own.
fn release_closed(registry: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = registry.py();
    let mut closed = Vec::new();
    for entry in registry.call_method0(intern!(py, "items"))?.try_iter()? {
        let (event_loop, port): (Bound<'_, PyAny>, Bound<'_, PyAny>) = entry?.extract()?;
        if let Ok(port) = port.cast_into::<Port>()
            && event_loop.call_method0(intern!(py, "is_closed"))?.is_truthy()?
        {
            closed.push(port);
        }
    }
    for port in closed {
        // Dropped after the lock is released (dropping Python objects may run code).
        let calls = std::mem::take(&mut *port.get().calls.futures.lock());
        drop(calls);
        let converting = std::mem::take(&mut port.get().calls.converting.lock().0);
        drop(converting);
    }
    Ok(())
}

fn create_port<'py>(event_loop: &Bound<'py, PyAny>, pid: u32) -> PyResult<Bound<'py, Port>> {
    let py = event_loop.py();
    let shared = Arc::new(Shared { finished: Mutex::new(Vec::new()), wake: OnceLock::new() });
    #[cfg(unix)]
    let (reader, writer) = {
        let (reader, writer) = UnixStream::pair()?;
        reader.set_nonblocking(true)?;
        writer.set_nonblocking(true)?;
        (reader, writer)
    };
    let port = Bound::new(
        py,
        Port {
            shared: Arc::clone(&shared),
            calls: Arc::new(Calls {
                futures: Mutex::new(HashMap::new()),
                converting: Mutex::new((VecDeque::new(), false)),
            }),
            next_id: AtomicU64::new(0),
            event_loop: PyWeakrefReference::new(event_loop)?.unbind(),
            pid,
        },
    )?;
    #[cfg(unix)]
    {
        let fd = reader.as_raw_fd();
        match event_loop.call_method1(intern!(py, "add_reader"), (fd, port.getattr(intern!(py, "drain"))?)) {
            Ok(_) => {
                let _ = shared.wake.set(Wake::Socket { reader, writer });
                return Ok(port);
            }
            Err(error) if error.is_instance_of::<PyNotImplementedError>(py) => {}
            Err(error) => return Err(error),
        }
    }
    let _ = shared.wake.set(Wake::Threadsafe {
        event_loop: PyWeakrefReference::new(event_loop)?.unbind(),
        port: PyWeakrefReference::new(port.as_any())?.unbind(),
    });
    Ok(port)
}

/// The running event loop (`RuntimeError` without one), or `ShutdownError` once the
/// interpreter is exiting.
fn running_loop(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    if is_shut_down() {
        return Err(ShutdownError::new_err("dynamoxy is shut down: the Python interpreter is exiting"));
    }
    GET_RUNNING_LOOP.import(py, "asyncio", "get_running_loop")?.call0()
}

/// Runs `fut` on the runtime and returns an asyncio future of the running loop for its outcome.
fn spawn<'py, F>(port: &Bound<'py, Port>, future: Bound<'py, PyAny>, fut: Pin<Box<F>>) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = Deferred> + Send + 'static,
{
    let py = port.py();
    let state = port.get();
    let id = state.next_id.fetch_add(1, Ordering::Relaxed);
    let shared = Arc::clone(&state.shared);
    let task = handle_for(state.pid).spawn(async move {
        let finished = CatchUnwind(fut).await;
        shared.finish(id, finished);
    });
    let registered = Py::new(py, Canceller { id, task: task.abort_handle(), calls: Arc::downgrade(&state.calls) })
        .and_then(|canceller| {
            future.call_method1(intern!(py, "add_done_callback"), (canceller.clone_ref(py),))?;
            Ok(canceller)
        });
    match registered {
        Ok(canceller) => {
            state.calls.futures.lock().insert(id, (future.clone().unbind(), canceller));
            Ok(future)
        }
        Err(error) => {
            task.abort();
            Err(error)
        }
    }
}

/// Converts a Rust future into an asyncio awaitable bound to the running event loop. The
/// future runs on the runtime.
pub fn future_into_py<F>(py: Python<'_>, fut: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = Deferred> + Send + 'static,
{
    future_into_py_in(py, std::process::id(), fut)
}

/// [`future_into_py`] for a caller that knows the current process id (saving a system call).
pub fn future_into_py_in<F>(py: Python<'_>, pid: u32, fut: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = Deferred> + Send + 'static,
{
    let event_loop = running_loop(py)?;
    let port = port_for(&event_loop, pid)?;
    let future = event_loop.call_method0(intern!(py, "create_future"))?;
    spawn(&port, future, Box::pin(fut))
}

/// Like [`future_into_py_in`], but polls `fut` once on the calling thread first (without the
/// GIL): when it is ready (in-memory operations), the awaitable is a [`Ready`] and the
/// runtime's threads are not involved.
pub fn future_into_py_inline<F>(py: Python<'_>, pid: u32, fut: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = Deferred> + Send + 'static,
{
    // Everything that can fail is done before the future runs.
    let event_loop = running_loop(py)?;
    let port = port_for(&event_loop, pid)?;
    let mut fut = Box::pin(fut);
    let runtime = handle_for(pid);
    let polled = py.detach(|| {
        let _context = runtime.enter();
        let mut cx = Context::from_waker(Waker::noop());
        catch_unwind(AssertUnwindSafe(|| fut.as_mut().poll(&mut cx)))
    });
    let finished = match polled {
        Ok(Poll::Pending) => {
            let future = event_loop.call_method0(intern!(py, "create_future"))?;
            return spawn(&port, future, fut);
        }
        Ok(Poll::Ready(deferred)) => Ok(deferred),
        Err(payload) => Err(panic_message(payload)),
    };
    Ok(Bound::new(py, Ready { state: Mutex::new((false, Some(finished))) })?.into_any())
}
