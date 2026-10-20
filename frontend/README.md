# BeeWork Web

React + TypeScript application for the BeeWork enterprise AI workspace.

## Stack

- React and React Router for the application shell and navigation.
- React Flow for visual workflow editing.
- Formily for schema-driven configuration forms.
- Vite for local development and production builds.

## Commands

```bash
npm install
npm run dev
npm run lint
npm run build
```

The Vite development server proxies `/api` and `/health` to `http://localhost:8000` by default. Docker Compose sets the target to the API service.
