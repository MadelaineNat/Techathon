//Welcome to Electronics University
//Subscribe us for learning something  new.

int soundSensor=2;
boolean LEDStatus=false;

void  setup() {
 pinMode(soundSensor,INPUT);
 Serial.begin(9600);

}

void  loop() {
  Serial.print("talk now");
  delay(2000);
  int SensorData=digitalRead(soundSensor);
  Serial.print(SensorData);
  if(SensorData==1){

    if(LEDStatus==false){
        LEDStatus=true;
        Serial.print("Turn on");
    }
    else{
        LEDStatus=false;
        Serial.print("Turn off");
    }
  }
 } 
