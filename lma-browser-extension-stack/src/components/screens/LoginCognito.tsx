import React from 'react';
import logo from './logo.svg';
import './LoginCognito.css';
import { Box, Button, Container, ContentLayout, Form, FormField, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';
import { useNavigation } from '../../context/NavigationContext';
import { useUserContext } from '../../context/UserContext';

function LoginCognito() {

  const { navigate } = useNavigation();
  const { login,loggedIn, exchangeCodeForToken } = useUserContext();

  const queryParameters = new URLSearchParams(window.location.search);
  const code = queryParameters.get("code");

  if (code && !loggedIn) {
    exchangeCodeForToken(code)
  }

  return (
    <ContentLayout header={
      <div></div>
    }>
      <Container
        fitHeight={true}
        footer={''}
      >
        <SpaceBetween size={'l'}>
          <div></div>
          <Grid gridDefinition={[{ colspan: 4, offset:4 }]}>
            <img className='logo' src='q_svg.svg'></img>
          </Grid>
          <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
            <SpaceBetween size={'xs'}>
              <h2 className='header'>Amazon Live<br/>Meeting Assistant</h2>
              <p className='headerDesc'>Powered by Amazon Transcribe and Q</p>
            </SpaceBetween>
          </Grid>
          <Grid gridDefinition={[{ colspan: 6, offset: 3 }]}>
            <Button variant='primary' fullWidth={true} onClick={() => login()}>Login</Button>
          </Grid>
        </SpaceBetween>
      </Container>
    </ContentLayout>
  );
}

export default LoginCognito;