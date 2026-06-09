module.exports = {
  apps: [{
    name: "skustock-bot",
    script: "bots.py",
    interpreter: "./venv/bin/python3",
    watch: false,
    autorestart: true,
    env_file: ".env",
  }]
}
