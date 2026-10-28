import React, { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  YAxis,
  XAxis,
  CartesianGrid,
  ResponsiveContainer,
} from "recharts";
import Control from "../components/Control";
import { useControlContext } from "../components/ControlContext";

const DerivacionesECG = () => {
  const {
    isRecording,
    wsConnected,
    derivationStats,
    getDerivationData,
    derivationDataVersion,
    clearDerivations
  } = useControlContext();

  const [heartRate, setHeartRate] = useState(0);
  const [showStats, setShowStats] = useState(false);

  // ========== CONFIGURACIÓN DE VENTANA DESLIZANTE ==========
  const WINDOW_SIZE = 1500; // 5 segundos a 1kHz
  const SAMPLING_RATE = 1000; // 1kHz
  // ==========================================================

  // Configuración de las 12 derivaciones
  const derivaciones = [
    { id: "I", name: "I", color: "#00DDFF", group: "bipolares" },
    { id: "II", name: "II", color: "#00FF88", group: "bipolares" },
    { id: "III", name: "III", color: "#FFD700", group: "bipolares" },
    { id: "aVR", name: "aVR", color: "#FF6B6B", group: "aumentadas" },
    { id: "aVL", name: "aVL", color: "#4ECDC4", group: "aumentadas" },
    { id: "aVF", name: "aVF", color: "#95E1D3", group: "aumentadas" },
    { id: "V1", name: "V1", color: "#A8E6CF", group: "precordiales" },
    { id: "V2", name: "V2", color: "#FFD3B6", group: "precordiales" },
    { id: "V3", name: "V3", color: "#FFAAA5", group: "precordiales" },
    { id: "V4", name: "V4", color: "#FF8B94", group: "precordiales" },
    { id: "V5", name: "V5", color: "#A8D8EA", group: "precordiales" },
    { id: "V6", name: "V6", color: "#AA96DA", group: "precordiales" }
  ];

  // ========== CÁLCULO AUTOMÁTICO DE FRECUENCIA CARDÍACA ==========
  useEffect(() => {
    const derivationII = getDerivationData('II');

    if (derivationII.length < 1000) {
      return;
    }

    const detectRPeaks = (data) => {
      const peaks = [];
      const threshold = 0.5;
      const minDistance = 400; // 400ms a 1kHz = 0.4s entre picos

      for (let i = minDistance; i < data.length - minDistance; i++) {
        const value = data[i].value;
        
        let isMax = true;
        for (let j = i - minDistance; j <= i + minDistance; j++) {
          if (j !== i && data[j].value > value) {
            isMax = false;
            break;
          }
        }

        if (isMax && value > threshold) {
          peaks.push(i);
        }
      }

      return peaks;
    };

    const peaks = detectRPeaks(derivationII);

    if (peaks.length >= 2) {
      let totalInterval = 0;
      for (let i = 1; i < peaks.length; i++) {
        totalInterval += peaks[i] - peaks[i - 1];
      }
      
      const avgInterval = totalInterval / (peaks.length - 1);
      const intervalSeconds = avgInterval / SAMPLING_RATE;
      const bpm = Math.round(60 / intervalSeconds);

      if (bpm >= 40 && bpm <= 200) {
        setHeartRate(bpm);
      }
    }
  }, [derivationDataVersion, getDerivationData]);

  // ========== FUNCIÓN PARA PREPARAR DATOS CON Δt CONSTANTE ==========
  const prepareChartData = (rawData) => {
    // Si tenemos más datos que la ventana, tomamos solo los últimos
    if (rawData.length >= WINDOW_SIZE) {
      const recentData = rawData.slice(-WINDOW_SIZE);
      return recentData.map((point, idx) => ({
        x: idx,
        value: point.value
      }));
    }
    
    // Si tenemos menos datos, creamos array completo con nulls al final
    const chartData = [];
    
    // Agregar datos reales
    for (let i = 0; i < rawData.length; i++) {
      chartData.push({
        x: i,
        value: rawData[i].value
      });
    }
    
    // Completar con nulls hasta WINDOW_SIZE
    for (let i = rawData.length; i < WINDOW_SIZE; i++) {
      chartData.push({
        x: i,
        value: null
      });
    }
    
    return chartData;
  };

  // ========== COMPONENTE PARA CADA TIRA DE ECG ==========
  const ECGStrip = ({ derivation, data, index }) => {
    const chartData = prepareChartData(data);
    
    // Calcular dominio Y basado solo en datos reales (no nulls)
    const realValues = data.map(d => d.value);
    const minValue = realValues.length > 0 ? Math.min(...realValues) : -1;
    const maxValue = realValues.length > 0 ? Math.max(...realValues) : 1;
    const padding = (maxValue - minValue) * 0.2 || 0.1;

    // Posición del cursor de barrido (solo cuando hay menos de WINDOW_SIZE datos)
    const sweepPosition = data.length < WINDOW_SIZE ? data.length : null;

    return (
      <div style={{ 
        height: '100px',
        backgroundColor: 'transparent',
        position: 'relative',
        borderBottom: index < 11 ? '1px solid #003344' : 'none',
        width: '100%',
        display: 'flex',
        flexDirection: 'row',
        gap: '5px',
        padding: '5px 10px'
      }}>
        {/* Label */}
        <div
          style={{
            width: '50px',
            flexShrink: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#FFFFFF',
            fontSize: '20px',
            fontWeight: 'bold',
            fontFamily: 'monospace',
            backgroundColor: 'rgba(0,0,0,0.7)',
            borderRadius: '8px',
            border: `2px solid ${derivation.color}`,
            flexDirection: 'column'
          }}
        >
          <div>{derivation.name}</div>
          {showStats && (
            <div style={{ fontSize: '8px', color: '#AAA' }}>
              {data.length}
            </div>
          )}
        </div>

        {/* Gráfica */}
        <div style={{ 
          width: '100%',
          height: '100%',
          minWidth: 0,
          position: 'relative'
        }}>
          {data.length === 0 ? (
            <div style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              height: '100%',
              color: '#666',
              fontSize: '11px'
            }}>
              Esperando datos de {derivation.name}...
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart 
                data={chartData} 
                margin={{ top: 5, right: 10, left: 0, bottom: 5 }}
              >
                <CartesianGrid 
                  strokeDasharray="1 1" 
                  stroke="#004466" 
                  strokeWidth={0.3}
                />
                <XAxis 
                  type="number"
                  dataKey="x"
                  domain={[0, WINDOW_SIZE - 1]}
                  tick={false}
                  axisLine={false}
                />
                <YAxis 
                  domain={[minValue - padding, maxValue + padding]}
                  tick={false}
                  axisLine={false}
                  width={0}
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke={derivation.color}
                  strokeWidth={1.8}
                  dot={false}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
          
          {/* Cursor de barrido estilo ECG médico */}
          {sweepPosition !== null && (
            <div
              style={{
                position: 'absolute',
                left: `${(sweepPosition / WINDOW_SIZE) * 100}%`,
                top: 0,
                bottom: 0,
                width: '3px',
                background: 'linear-gradient(to right, transparent, rgba(255,255,255,0.9), transparent)',
                pointerEvents: 'none',
                boxShadow: '0 0 15px rgba(255,255,255,0.7)',
                zIndex: 10
              }}
            />
          )}
        </div>
      </div>
    );
  };

  const handleClearAll = () => {
    if (window.confirm("¿Limpiar todas las gráficas? Esta acción no se puede deshacer.")) {
      clearDerivations();
    }
  };

  const totalSamples = Object.values(derivationStats).reduce((sum, stat) => sum + (stat.samples || 0), 0);
  const totalBatches = Object.values(derivationStats).reduce((sum, stat) => sum + (stat.batches || 0), 0);
  
  const derivationsWithData = derivaciones.filter(d => {
    const data = getDerivationData(d.id);
    return data && data.length > 0;
  }).length;

  const windowTimeSeconds = (WINDOW_SIZE / SAMPLING_RATE).toFixed(1);

  return (
    <div style={{ 
      width: '100vw',
      height: '100vh', 
      backgroundColor: "#000000",
      color: "#FFFFFF",
      fontFamily: 'monospace',
      overflow: 'hidden',
      position: 'relative'
    }}>
      <Control />

      {/* Indicador de estado */}
      <div style={{
        position: 'absolute',
        top: '70px',
        right: '10px',
        zIndex: 1000,
        backgroundColor: 'rgba(0,0,0,0.9)',
        padding: '8px 12px',
        borderRadius: '8px',
        border: `2px solid ${wsConnected ? '#00FF88' : '#FF6B6B'}`,
        fontSize: '9px'
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px' }}>
          <div style={{
            width: '10px',
            height: '10px',
            borderRadius: '50%',
            backgroundColor: wsConnected ? '#00FF88' : '#FF6B6B',
            animation: wsConnected ? 'pulse 2s infinite' : 'none'
          }}></div>
          <div style={{ fontSize: '11px', fontWeight: 'bold' }}>
            {wsConnected ? 'CONECTADO' : 'DESCONECTADO'}
          </div>
        </div>
        
        <div style={{ color: '#AAA', lineHeight: '1.4' }}>
          <div>📊 {derivationsWithData}/12 derivaciones</div>
          <div>📈 {totalSamples.toLocaleString()} muestras</div>
          <div>📦 {totalBatches} batches</div>
          <div style={{ color: '#00DDFF' }}>
            🪟 {WINDOW_SIZE} pts ({windowTimeSeconds}s)
          </div>
          {heartRate > 0 && (
            <div style={{ color: '#00FF88', fontWeight: 'bold', fontSize: '11px', marginTop: '3px' }}>
              ❤️ {heartRate} bpm
            </div>
          )}
        </div>
      </div>

      {/* Panel de derivaciones */}
      <div style={{ 
        height: 'calc(100vh - 110px)',
        backgroundColor: '#000000',
        paddingTop: '10px',
        overflow: 'auto',
        width: '100%'
      }}>
        {derivaciones.map((derivacion, index) => (
          <ECGStrip 
            key={derivacion.id}
            derivation={derivacion} 
            data={getDerivationData(derivacion.id)}
            index={index}
          />
        ))}
      </div>

      {/* Panel inferior */}
      <div style={{
        position: 'absolute',
        bottom: 0,
        left: 0,
        right: 0,
        backgroundColor: '#001122',
        borderTop: '1px solid #004466',
        padding: '5px 15px',
        height: '40px',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        fontSize: '11px'
      }}>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <button
            style={{
              backgroundColor: '#660000',
              color: '#FF6B6B',
              border: '1px solid #FF6B6B',
              padding: '4px 10px',
              borderRadius: '4px',
              fontSize: '11px',
              fontWeight: 'bold',
              cursor: 'pointer',
              fontFamily: 'monospace'
            }}
            onClick={handleClearAll}
            onMouseOver={(e) => e.target.style.backgroundColor = '#880000'}
            onMouseOut={(e) => e.target.style.backgroundColor = '#660000'}
          >
            🗑️ LIMPIAR
          </button>
          
          <button
            style={{
              backgroundColor: '#003366',
              color: '#00DDFF',
              border: '1px solid #00DDFF',
              padding: '4px 10px',
              borderRadius: '4px',
              fontSize: '11px',
              fontWeight: 'bold',
              cursor: 'pointer',
              fontFamily: 'monospace'
            }}
            onClick={() => setShowStats(!showStats)}
            onMouseOver={(e) => e.target.style.backgroundColor = '#004477'}
            onMouseOut={(e) => e.target.style.backgroundColor = '#003366'}
          >
            {showStats ? '📊 -' : '📊 +'}
          </button>

          <button
            style={{
              backgroundColor: '#003366',
              color: '#00DDFF',
              border: '1px solid #00DDFF',
              padding: '4px 10px',
              borderRadius: '4px',
              fontSize: '11px',
              fontWeight: 'bold',
              cursor: 'pointer',
              fontFamily: 'monospace'
            }}
            onMouseOver={(e) => e.target.style.backgroundColor = '#004477'}
            onMouseOut={(e) => e.target.style.backgroundColor = '#003366'}
          >
            💾 GUARDAR
          </button>
        </div>

        <div style={{ color: '#CCCCCC', fontSize: '10px' }}>
          {isRecording ? (
            <span style={{ color: '#00FF88', fontWeight: 'bold' }}>
              🔴 GRABANDO | {totalSamples.toLocaleString()} muestras
            </span>
          ) : (
            <span>{SAMPLING_RATE}Hz • 3.3V • {windowTimeSeconds}s VISIBLE</span>
          )}
        </div>
        
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          {heartRate > 0 && (
            <div style={{
              backgroundColor: 'rgba(0,255,136,0.2)',
              padding: '4px 10px',
              borderRadius: '4px',
              border: '1px solid #00FF88',
              fontSize: '12px',
              fontWeight: 'bold',
              color: '#00FF88'
            }}>
              ❤️ {heartRate} bpm
            </div>
          )}
          
          <div style={{ fontSize: '10px', color: '#666' }}>
            {WINDOW_SIZE}pts • {windowTimeSeconds}s
          </div>
        </div>
      </div>

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.7; transform: scale(1.1); }
        }
      `}</style>
    </div>
  );
};

export default DerivacionesECG;