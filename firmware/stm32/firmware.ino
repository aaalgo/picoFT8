#include <Arduino.h>
#include <Wire.h>
#include <si5351.h>

// ----------------------------------------------------
// STM32F103C8T6
//
// CH340:
//   PA9  = USART1 TX
//   PA10 = USART1 RX
//
// Si5351:
//   PB6 = SCL
//   PB7 = SDA
// ----------------------------------------------------

// Explicitly use USART1 connected to the onboard CH340.
// Constructor order: RX, TX

Si5351 si5351;

String commandBuffer;


// ----------------------------------------------------
// Serial replies
// ----------------------------------------------------

void replyOK()
{
    Serial1.println("OK");
}

void replyError(const char *msg)
{
    Serial1.print("ERR ");
    Serial1.println(msg);
}


// ----------------------------------------------------
// Command processor
// ----------------------------------------------------

void processCommand(String cmd)
{
    cmd.trim();

    if (cmd.length() == 0)
        return;


    // ---------------- PING ----------------
    if (cmd == "PING")
    {
        replyOK();
        return;
    }


    // ---------------- RF ON ----------------
    if (cmd == "ON")
    {
        si5351.output_enable(SI5351_CLK0, 1);
        replyOK();
        return;
    }


    // ---------------- RF OFF ----------------
    if (cmd == "OFF")
    {
        si5351.output_enable(SI5351_CLK0, 0);
        replyOK();
        return;
    }


    // ---------------- SET FREQUENCY ----------------
    //
    // ft8.py sends for example:
    //
    // F 1407500000
    //
    // Etherkit Si5351 frequency unit = 0.01 Hz
    //
    // 1407500000 = 14.07500000 MHz
    //
    if (cmd.startsWith("F "))
    {
        const char *p = cmd.c_str() + 2;

        char *endptr = nullptr;
        uint64_t freq = strtoull(p, &endptr, 10);

        if (p == endptr || *endptr != '\0')
        {
            replyError("BAD_FREQ");
            return;
        }

        // Safety limit: 20 m amateur band neighborhood.
        // Units are 0.01 Hz.
        if (freq < 1400000000ULL ||
            freq > 1435000000ULL)
        {
            replyError("OUT_OF_RANGE");
            return;
        }
#if 1 
        si5351.set_freq(freq, SI5351_CLK0);
#else
uint32_t t0 = micros();

si5351.set_freq(freq, SI5351_CLK0);

uint32_t dt = micros() - t0;

Serial1.print("TIME ");
Serial1.println(dt);
#endif
        replyOK();
        return;
    }


    replyError("UNKNOWN_COMMAND");
}


// ----------------------------------------------------
// Setup
// ----------------------------------------------------

void setup()
{
    // USART1 -> onboard CH340 -> USB-C
    Serial1.begin(115200);


    // I2C pins
    Wire.setSDA(PB7);
    Wire.setSCL(PB6);
    Wire.begin();

    delay(100);


    // Typical Si5351 breakout:
    // 25 MHz crystal, 8 pF crystal load.
    si5351.init(
        SI5351_CRYSTAL_LOAD_8PF,
        0,
        0
    );


    // Strong output drive into SN74ACT32 buffer
    si5351.drive_strength(
        SI5351_CLK0,
        SI5351_DRIVE_8MA
    );


    // Disable unused outputs
    si5351.output_enable(SI5351_CLK1, 0);
    si5351.output_enable(SI5351_CLK2, 0);


    // Very important:
    // transmitter starts with RF OFF
    si5351.output_enable(SI5351_CLK0, 0);


    commandBuffer.reserve(48);
}


// ----------------------------------------------------
// Main loop
// ----------------------------------------------------

void loop()
{
    while (Serial1.available())
    {
        char c = Serial1.read();

        if (c == '\n')
        {
            processCommand(commandBuffer);
            commandBuffer = "";
        }
        else if (c != '\r')
        {
            if (commandBuffer.length() < 47)
            {
                commandBuffer += c;
            }
            else
            {
                // Malformed/too-long input: discard
                commandBuffer = "";
            }
        }
    }
}