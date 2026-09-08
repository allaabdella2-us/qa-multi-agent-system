FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

ENV VITE_API_TARGET=http://api:8000
EXPOSE 5173

CMD ["npm", "run", "preview"]
