// pm2-конфиг для локального запуска обоих ботов.
// Запуск из корня репо:  pm2 start ecosystem.config.cjs
// Требуется локально (в GitHub НЕ коммитится):
//   - venv/              — Python-зависимости task-бота
//   - gpt-remote/node_modules/ — npm install для GPT-бота
//   - .env и gpt-remote/.env    — токены и ключи
module.exports = {
  apps: [
    {
      name: "pastila-task-bot",
      cwd: "./",
      script: "bot.py",
      interpreter: "./venv/bin/python",
      autorestart: true,
      max_restarts: 10,
    },
    {
      name: "pastila-gpt-remote",
      cwd: "./gpt-remote",
      script: "index.mjs",
      interpreter: "node",
      autorestart: true,
      max_restarts: 10,
    },
  ],
};
