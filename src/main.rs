use std::process::ExitCode;

#[tokio::main]
async fn main() -> ExitCode {
    match codexbot::cli::run().await {
        Ok(code) => ExitCode::from(code.clamp(0, u8::MAX as i32) as u8),
        Err(error) => {
            let detail = codexbot::security::redact_secrets(&error.to_string());
            eprintln!("错误：{}", detail.replace(['\r', '\n'], " "));
            ExitCode::FAILURE
        }
    }
}
