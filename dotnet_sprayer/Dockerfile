# Build from the repo root: docker build -f dotnet_sprayer/Dockerfile .
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build

WORKDIR /src
COPY dotnet_sprayer/Sprayer.csproj dotnet_sprayer/nuget.config ./
RUN dotnet restore
COPY dotnet_sprayer/Program.cs ./
RUN dotnet publish -c Release --no-restore -o /out

FROM mcr.microsoft.com/dotnet/runtime:8.0

WORKDIR /app
COPY --from=build /out ./
ENTRYPOINT ["dotnet", "Sprayer.dll"]
