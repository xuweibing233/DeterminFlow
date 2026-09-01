use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
// Fork baseline: cold starts on Windows routinely take 40-60s (5 PyInstaller
// processes booting serially, each rescanning the full session catalog, plus
// Defender scanning of unsigned binaries). 60s was tight enough to fail
// spuriously; the backend does eventually become ready, so wait longer.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(180);

pub struct BackendState {
    child: Mutex<Option<Child>>,
}

pub struct LaunchedBackend {
    pub child: Child,
    pub url: String,
}

impl BackendState {
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
        }
    }

    pub fn track(&self, mut child: Child) -> Result<(), String> {
        let Ok(mut guard) = self.child.lock() else {
            terminate_child(&mut child);
            return Err("无法记录内置后端进程".to_string());
        };
        if guard.is_some() {
            terminate_child(&mut child);
            return Err("内置后端进程已经启动".to_string());
        }
        *guard = Some(child);
        Ok(())
    }

    pub fn stop(&self) -> bool {
        let Ok(mut guard) = self.child.lock() else {
            return false;
        };
        let Some(mut child) = guard.take() else {
            return false;
        };
        terminate_child(&mut child);
        true
    }
}

impl Drop for BackendState {
    fn drop(&mut self) {
        self.stop();
    }
}

fn terminate_child(child: &mut Child) {
    #[cfg(target_os = "windows")]
    {
        let pid = child.id().to_string();
        let _ = Command::new("taskkill")
            .args(["/PID", pid.as_str(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

pub fn launch(app: &AppHandle) -> Result<LaunchedBackend, String> {
    let user_root = app
        .path()
        .app_local_data_dir()
        .map_err(|error| format!("无法解析用户数据目录: {error}"))?;
    let logs_dir = user_root.join("logs");
    fs::create_dir_all(&logs_dir).map_err(|error| format!("无法创建桌面日志目录: {error}"))?;

    let backend_path = resolve_backend_path(app)?;
    if !backend_path.is_file() {
        return Err(format!("内置后端不存在: {}", backend_path.display()));
    }

    let port = reserve_local_port()?;
    let log_path = logs_dir.join("backend-console.log");
    let stdout = open_log(&log_path)?;
    let stderr = stdout
        .try_clone()
        .map_err(|error| format!("无法复制后端日志句柄: {error}"))?;

    let mut command = Command::new(&backend_path);
    command
        .arg("--port")
        .arg(port.to_string())
        .arg("--user-data-dir")
        .arg(&user_root)
        .current_dir(&user_root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    if let Some(backend_dir) = backend_path.parent() {
        let mut paths = vec![backend_dir.to_path_buf()];
        if let Some(current_path) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&current_path));
        }
        if let Ok(runtime_path) = std::env::join_paths(paths) {
            command.env("PATH", runtime_path);
        }
    }

    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);

    let child = command
        .spawn()
        .map_err(|error| format!("无法启动内置后端: {error}"))?;
    Ok(LaunchedBackend {
        child,
        url: format!("http://127.0.0.1:{port}"),
    })
}

pub fn wait_until_ready(base_url: &str) -> Result<(), String> {
    let address = base_url
        .strip_prefix("http://")
        .ok_or_else(|| format!("无效的本地服务地址: {base_url}"))?;
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    let mut last_error = String::new();

    while Instant::now() < deadline {
        match request_status(address) {
            Ok(status) if status == 200 => return Ok(()),
            Ok(status) => last_error = format!("状态接口返回 HTTP {status}"),
            Err(error) => last_error = error,
        }
        thread::sleep(Duration::from_millis(250));
    }

    Err(format!("本地服务未在 60 秒内就绪: {last_error}"))
}

fn request_status(address: &str) -> Result<u16, String> {
    let mut stream = TcpStream::connect_timeout(
        &address
            .parse()
            .map_err(|error| format!("无法解析本地服务地址: {error}"))?,
        Duration::from_secs(1),
    )
    .map_err(|error| error.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|error| error.to_string())?;
    stream
        .write_all(
            b"GET /api/system/status HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
        )
        .map_err(|error| error.to_string())?;

    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|error| error.to_string())?;
    let status = response
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| "状态接口返回了无效响应".to_string())?;
    Ok(status)
}

fn reserve_local_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|error| format!("无法分配本地端口: {error}"))?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| format!("无法读取本地端口: {error}"))
}

fn resolve_backend_path(app: &AppHandle) -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("DETERMINFLOW_DESKTOP_BACKEND") {
        return Ok(PathBuf::from(path));
    }
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法解析安装资源目录: {error}"))?;
    let executable = if cfg!(target_os = "windows") {
        "determinflow-backend.exe"
    } else {
        "determinflow-backend"
    };
    Ok(resource_dir
        .join("runtime")
        .join("backend")
        .join(executable))
}

fn open_log(path: &Path) -> Result<std::fs::File, String> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("无法打开后端日志 {}: {error}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn spawn_long_running_child() -> Child {
        #[cfg(target_os = "windows")]
        {
            let mut command = Command::new("cmd");
            command
                .args(["/C", "ping 127.0.0.1 -n 30 > nul"])
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            return command.spawn().expect("test child should start");
        }

        #[cfg(not(target_os = "windows"))]
        Command::new("sh")
            .args(["-c", "sleep 30"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("test child should start")
    }

    #[test]
    fn reserves_a_loopback_port() {
        let port = reserve_local_port().expect("port should be available");
        assert!(port > 0);
        TcpListener::bind(("127.0.0.1", port)).expect("reserved port should be released");
    }

    #[test]
    fn rejects_non_http_backend_urls() {
        let error = wait_until_ready("https://127.0.0.1:1").unwrap_err();
        assert!(error.contains("无效的本地服务地址"));
    }

    #[test]
    fn stops_the_tracked_backend_once() {
        let state = BackendState::new();
        state
            .track(spawn_long_running_child())
            .expect("backend should be tracked");

        assert!(state.stop());
        assert!(!state.stop());
    }

    #[test]
    fn rejects_and_terminates_a_second_tracked_backend() {
        let state = BackendState::new();
        state
            .track(spawn_long_running_child())
            .expect("first backend should be tracked");

        let error = state
            .track(spawn_long_running_child())
            .expect_err("second backend should be rejected");
        assert!(error.contains("已经启动"));
        assert!(state.stop());
    }
}
